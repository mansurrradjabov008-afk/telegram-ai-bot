"""
Unit and integration tests for Smart Digest V2 - Phase 5:
- Formatting & Presentation Engine (DigestFormatter)
- Section ordering (F, E, C, B, A, D)
- Empty section fallback ("Bu mavzuda yangilik topilmadi")
- Section D deterministic format & Section F deadline display
- Footer counters (spam, discarded, limits)
- Message splitting under 4000 characters without breaking HTML tags or items
- Single topic formatting (/digest topic <A-F>)
- Debug page formatting & pagination (/digest debug)
- Concurrency guard in DigestService ("Tayyorlanmoqda")
- Database CRUD queries for runs & excluded posts
- Daily scheduler registration & catch-up logic (<= 12h runs, > 12h skips)
"""

import pytest
import asyncio
import html
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytz

from database.connection import init_db
from database.models import DigestPost
from database.crud import DatabaseManager
from services.digest_summarizer import DigestItem
from services.digest_formatter import DigestFormatter, SECTION_METADATA
from services.digest_service import DigestService
from services.scheduler_service import SchedulerService
from config import settings


def test_formatter_header_uzbek_date():
    """Verifies header formats date in Uzbek Latin and includes analyzed/selected counts."""
    # Test with fixed date: 17-aprel, 2026-yil
    dt = datetime(2026, 4, 17, 10, 0, tzinfo=pytz.timezone(settings.TIMEZONE))
    header = DigestFormatter.format_header(analyzed_count=42, selected_count=7, target_date=dt)

    assert "📅 <b>17-aprel, 2026-yil</b>" in header
    assert "42 ta post tahlil qilindi" in header
    assert "7 ta tanlandi" in header
    assert "━━━━━━━━━━━━━━━━━━━━" in header


def test_formatter_section_order_and_empty_sections():
    """
    Verifies full digest output follows strict section order: F, E, C, B, A, D.
    Verifies empty sections display 'Bu mavzuda yangilik topilmadi'.
    """
    sections = {
        "A": [],
        "B": [],
        "C": [],
        "D": [],
        "E": [],
        "F": [],
    }
    messages = DigestFormatter.format_full_digest(
        sections=sections,
        analyzed_count=10,
        selected_count=0,
        spam_count=3,
        discarded_by_verification=1,
    )

    assert len(messages) >= 1
    full_text = "\n".join(messages)

    # Check each section title is present
    for code in ["F", "E", "C", "B", "A", "D"]:
        meta = SECTION_METADATA[code]
        assert meta["title"] in full_text

    # Check order of sections in text: F comes before E, E before C, C before B, B before A, A before D
    pos_f = full_text.find(SECTION_METADATA["F"]["title"])
    pos_e = full_text.find(SECTION_METADATA["E"]["title"])
    pos_c = full_text.find(SECTION_METADATA["C"]["title"])
    pos_b = full_text.find(SECTION_METADATA["B"]["title"])
    pos_a = full_text.find(SECTION_METADATA["A"]["title"])
    pos_d = full_text.find(SECTION_METADATA["D"]["title"])

    assert 0 <= pos_f < pos_e < pos_c < pos_b < pos_a < pos_d

    # All empty sections should contain the fallback phrase
    empty_phrase_count = full_text.count("Bu mavzuda yangilik topilmadi")
    assert empty_phrase_count == 6


def test_formatter_section_d_and_f_items():
    """
    Verifies Section D renders deterministic vacancy format with disclaimer.
    Verifies Section F displays application deadline.
    """
    item_d = DigestItem(
        topic="D",
        title="Python Dasturchi",
        summary="Python Dasturchi — $2000 - $3000/oyiga — Joylashuv: Toshkent",
        post_ids_used=[101],
        source_links=[("IT Jobs", "https://t.me/itjobs/101")],
        post_date="2026-04-17",
        labels=["e'londa ko'rsatilgan, tasdiqlanmagan"],
    )

    formatted_d = DigestFormatter.format_item(item_d)
    assert "• <b>Python Dasturchi</b>" in formatted_d
    assert "Python Dasturchi — $2000 - $3000/oyiga — Joylashuv: Toshkent" in formatted_d
    assert '<a href="https://t.me/itjobs/101">[IT Jobs]</a>' in formatted_d
    assert "📅 2026-04-17" in formatted_d
    expected_label = html.escape("e'londa ko'rsatilgan, tasdiqlanmagan")
    assert f"<i>{expected_label}</i>" in formatted_d

    item_f = DigestItem(
        topic="F",
        title="Fulbright Stipendiyasi 2026",
        summary="AQShda magistratura bosqichi uchun to'liq grant dasturi e'lon qilindi.",
        post_ids_used=[202],
        source_links=[("Grantlar Uz", "https://t.me/grantlar/202")],
        post_date="2026-04-16",
        deadline="2026-05-15",
    )

    formatted_f = DigestFormatter.format_item(item_f)
    assert "• <b>Fulbright Stipendiyasi 2026</b>" in formatted_f
    assert html.escape("AQShda magistratura bosqichi uchun to'liq grant") in formatted_f
    assert "⏳ <b>Muddati:</b> 2026-05-15" in formatted_f
    assert '<a href="https://t.me/grantlar/202">[Grantlar Uz]</a>' in formatted_f


def test_formatter_footer_counters():
    """Verifies footer formatting with spam, discarded, and limit counters."""
    footer = DigestFormatter.format_footer(
        spam_count=12,
        discarded_by_verification=4,
        skipped_by_limit=2,
        failed_topics=["A"],
    )

    assert "🛡 <i>Reklama/spam filtrlandi: 12 ta</i>" in footer
    assert "🔍 <i>Tekshiruvdan o'tmagan: 4 ta</i>" in footer
    assert "⚠️ <i>Limit sababli kiritilmagan: 2 ta</i>" in footer
    assert "⚠️ <i>Muvaffaqiyatsiz mavzular: A</i>" in footer


def test_formatter_message_splitting_under_4000_chars():
    """
    Verifies that when digest content exceeds 4000 characters, it splits safely
    at item or section boundaries, never exceeding 4000 characters per message.
    """
    # Create 25 realistic items across topics
    sections = {"F": [], "E": [], "C": [], "B": [], "A": [], "D": []}
    for i in range(25):
        topic = ["F", "E", "C", "B", "A", "D"][i % 6]
        item = DigestItem(
            topic=topic,
            title=f"Muhim voqea sarlavhasi raqami #{i + 1} batafsil ma'lumotlar bilan",
            summary=(
                f"Bu yerda #{i + 1}-voqea haqida batafsil tushuntirish va axborot bayon etilgan. "
                f"Ushbu xulosada voqea mohiyati va tafsilotlari qisqacha yoritib berilgan bo'lib, "
                f"o'quvchi uchun barcha asosiy ma'lumotlarni o'z ichiga oladi."
            ),
            post_ids_used=[i + 1000],
            source_links=[(f"Kanal {i}", f"https://t.me/channel_{i}/{i}")],
            post_date="2026-04-17",
            deadline="2026-05-01" if topic == "F" else None,
        )
        sections[topic].append(item)

    messages = DigestFormatter.format_full_digest(
        sections=sections,
        analyzed_count=100,
        selected_count=25,
        spam_count=15,
        discarded_by_verification=3,
    )

    assert len(messages) > 1  # Should have been split into at least 2 messages

    for idx, msg in enumerate(messages):
        assert len(msg) <= 4000, f"Message chunk {idx} exceeded 4000 chars: {len(msg)}"
        # Verify tag balancing (no unclosed tags)
        assert msg.count("<b>") == msg.count("</b>"), f"Mismatched <b> tags in chunk {idx}"
        assert msg.count("<i>") == msg.count("</i>"), f"Mismatched <i> tags in chunk {idx}"
        assert msg.count("<a ") == msg.count("</a>"), f"Mismatched <a> tags in chunk {idx}"


def test_formatter_single_topic_view():
    """Verifies format_single_topic outputs only the selected topic."""
    item = DigestItem(
        topic="E",
        title="Gemini 2.5 Pro e'lon qilindi",
        summary="Google yangi avlod modellar oilasini rasman taqdim etdi.",
        post_ids_used=[501],
        source_links=[("AI News", "https://t.me/ainews/501")],
        post_date="2026-04-17",
    )

    messages = DigestFormatter.format_single_topic(
        topic="E",
        items=[item],
        analyzed_count=20,
    )

    assert len(messages) >= 1
    text = "\n".join(messages)
    assert "SUN'IY INTELLEKT (AI)" in text
    assert html.escape("Gemini 2.5 Pro e'lon qilindi") in text
    assert "Mavzu E" in text
    # Other sections should not be present
    assert "IMKONIYATLAR" not in text
    assert "KASBLAR NARXLARI" not in text


def test_formatter_debug_posts_page_truncation():
    """Verifies debug page pagination and ~100 char text truncation."""
    posts = [
        DigestPost(
            id=1,
            chat_id=-100123456,
            message_id=10,
            chat_title="E'lonlar Bozori",
            chat_username="elonlar",
            text="A" * 250,  # Long text
            status="filtered",
            discard_reason="spam_high_score: 0.95",
        ),
        DigestPost(
            id=2,
            chat_id=-100123456,
            message_id=11,
            chat_title="Kanal 2",
            chat_username=None,
            text="Qisqa matn",
            status="discarded",
            discard_reason="verification_number_mismatch",
        ),
    ]

    debug_page = DigestFormatter.format_debug_posts_page(
        posts=posts,
        page=0,
        total_count=35,
        page_size=10,
    )

    assert "SMART DIGEST V2 — CHETLATILGAN POSTLAR (DEBUG)" in debug_page
    assert "<b>Sahifa:</b> <code>1 / 4</code>" in debug_page
    assert "Jami: <code>35</code> ta" in debug_page
    assert "ID: 1" in debug_page
    assert "spam_high_score: 0.95" in debug_page
    assert "verification_number_mismatch" in debug_page
    # First post text must be truncated to ~100 chars + "..."
    assert "A" * 100 + "..." in debug_page
    assert "A" * 200 not in debug_page


@pytest.mark.asyncio
async def test_digest_service_concurrency_guard():
    """Verifies concurrency guard rejects concurrent digest executions with in_progress status."""
    db_mock = MagicMock()
    ai_mock = MagicMock()
    service = DigestService(db_mock, ai_mock)

    # Manually lock progress
    service._is_run_in_progress = True

    result = await service.run_smart_digest(trigger="manual")
    assert result["status"] == "in_progress"
    assert "Smart Digest V2 hozirda tayyorlanmoqda!" in result["messages"][0]

    # Release lock
    service._is_run_in_progress = False


@pytest.mark.asyncio
async def test_database_has_scheduled_run_today_and_debug_posts(tmp_path):
    """Verifies has_scheduled_digest_run_today and get_excluded_digest_posts in DatabaseManager."""
    db_file = tmp_path / "test_phase5.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Initially False
    assert await db_manager.has_scheduled_digest_run_today(today_str) is False

    # Create scheduled run
    run_id = await db_manager.create_digest_run(trigger="scheduled")
    await db_manager.finish_digest_run(run_id, status="completed", stats={"total_collected": 5})

    # Now should be True
    assert await db_manager.has_scheduled_digest_run_today(today_str) is True

    # Insert posts with filtered and discarded status
    p1 = DigestPost(
        chat_id=1,
        message_id=101,
        chat_title="Kanal A",
        text="Spam e'lon",
        status="filtered",
        discard_reason="spam_keyword",
        digest_run_id=run_id,
    )
    p2 = DigestPost(
        chat_id=1,
        message_id=102,
        chat_title="Kanal B",
        text="Gumonli e'lon",
        status="discarded",
        discard_reason="verification_failed",
        digest_run_id=run_id,
    )
    p3 = DigestPost(
        chat_id=1,
        message_id=103,
        chat_title="Kanal C",
        text="Toza yangilik",
        status="included",
        digest_run_id=run_id,
    )
    await db_manager.bulk_save_digest_posts([p1, p2, p3])

    # Query excluded posts
    excluded, total = await db_manager.get_excluded_digest_posts(run_id=run_id, offset=0, limit=10)
    assert total == 2
    assert len(excluded) == 2
    assert {p.status for p in excluded} == {"filtered", "discarded"}


@pytest.mark.asyncio
async def test_scheduler_catchup_logic(tmp_path):
    """
    Verifies catch-up rule:
    - Runs if late <= 12 hours
    - Skips if late > 12 hours
    - Skips if already sent today
    """
    db_file = tmp_path / "test_sched.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    scheduler_service = SchedulerService(db_manager)
    digest_service_mock = MagicMock()
    digest_service_mock.run_smart_digest = AsyncMock(return_value={"status": "completed", "messages": ["Digest test"]})

    notify_mock = AsyncMock()

    tz = pytz.timezone(settings.TIMEZONE)

    # Case 1: Scheduled time was 2 hours ago (within 12h) -> Should execute catch-up
    now_tz = datetime.now(tz)
    two_hours_ago = (now_tz - timedelta(hours=2)).strftime("%H:%M")

    with patch.object(db_manager, "has_scheduled_digest_run_today", new=AsyncMock(return_value=False)):
        await scheduler_service.check_and_run_catchup_digest(
            digest_service=digest_service_mock,
            notify_boss_callback=notify_mock,
            digest_time_str=two_hours_ago,
        )
        assert digest_service_mock.run_smart_digest.called
        assert notify_mock.called

    # Reset mocks
    digest_service_mock.run_smart_digest.reset_mock()
    notify_mock.reset_mock()

    # Case 2: Scheduled time was 14 hours ago (> 12h cutoff) -> Should skip
    fourteen_hours_ago = (now_tz - timedelta(hours=14)).strftime("%H:%M")

    with patch.object(db_manager, "has_scheduled_digest_run_today", new=AsyncMock(return_value=False)):
        await scheduler_service.check_and_run_catchup_digest(
            digest_service=digest_service_mock,
            notify_boss_callback=notify_mock,
            digest_time_str=fourteen_hours_ago,
        )
        assert not digest_service_mock.run_smart_digest.called
        assert not notify_mock.called

    # Case 3: Already sent today -> Should skip immediately
    with patch.object(db_manager, "has_scheduled_digest_run_today", new=AsyncMock(return_value=True)):
        await scheduler_service.check_and_run_catchup_digest(
            digest_service=digest_service_mock,
            notify_boss_callback=notify_mock,
            digest_time_str=two_hours_ago,
        )
        assert not digest_service_mock.run_smart_digest.called
        assert not notify_mock.called
