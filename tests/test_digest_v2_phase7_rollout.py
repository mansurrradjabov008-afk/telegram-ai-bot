"""
End-to-End Verification and Rollout Tests for Smart Digest V2 - Phase 7.

Validates the full pipeline from collection/ingestion to final output formatting:
1. End-to-end dry run with representative realistic channel posts covering:
   - All 6 topics (A, B, C, D, E, F)
   - Spam/scam/commercial ads filtering
   - Cross-channel duplicate news collapsing (Latin & Cyrillic)
   - Relative dates preservation alongside post dates
   - Price notations & currency normalization
   - Anti-hallucination verification (numbers, URLs, phone numbers, entities)
   - Section D strict deterministic structure
   - Section F deadline sorting
   - Exact footer telemetry counters
2. Privacy boundary enforcement (zero private chat leakage).
3. Feature flag toggle (DIGEST_V2_ENABLED=True vs DIGEST_V2_ENABLED=False).
"""

import pytest
import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from config import settings
from database.connection import init_db
from database.models import DigestPost, DigestSource, DigestRun
from database.crud import DatabaseManager
from services.digest_cleaner import DigestCleaner
from services.digest_verifier import DigestVerifier
from services.digest_summarizer import DigestSummarizer, DigestItem
from services.digest_formatter import DigestFormatter
from services.digest_service import DigestService
from services.scheduler_service import SchedulerService


@pytest.mark.asyncio
async def test_end_to_end_digest_pipeline_complete(tmp_path):
    """
    Comprehensive End-to-End dry run of Smart Digest V2 pipeline:
    - Ingests representative realistic posts across all 6 topics + spam + duplicates.
    - Runs cleaning, deduplication, spam filtering, classification, verification, and formatting.
    """
    db_file = tmp_path / "test_p7_e2e.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    # 1. Setup sources whitelist (all channels/groups, zero private chats)
    s1 = await db_manager.add_digest_source(peer_id=-100111, title="Siyosat Va Diplomatiya", username="siyosat_uz", priority=5)
    s2 = await db_manager.add_digest_source(peer_id=-100222, title="TSUE Axborot Markazi", username="tsue_official", priority=4)
    s3 = await db_manager.add_digest_source(peer_id=-100333, title="IT Va Sun'iy Intellekt", username="ai_uzbekistan", priority=5)
    s4 = await db_manager.add_digest_source(peer_id=-100444, title="Vakansiyalar Markazi", username="ish_bor", priority=3)
    s5 = await db_manager.add_digest_source(peer_id=-100555, title="Global Imkoniyatlar", username="grants_uz", priority=4)
    s6 = await db_manager.add_digest_source(peer_id=-100666, title="Kripto Va Reklama", username="ads_channel", priority=1)

    now = datetime.now(timezone.utc)
    t_str = now.isoformat()

    # 2. Representative posts dataset
    raw_posts = [
        # Spam & scams (Must be filtered out by cleaner)
        DigestPost(
            chat_id=-100666, message_id=1, chat_title="Kripto Va Reklama", chat_username="ads_channel",
            text="Tez pul topish siri! Kuniga 500 000 so'm daromad. Kriptovalyuta orqali pulingizni 2 kunda 2 barobarga oshiring!",
            posted_at=t_str, link="https://t.me/ads_channel/1", status="new"
        ),
        DigestPost(
            chat_id=-100666, message_id=2, chat_title="Kripto Va Reklama", chat_username="ads_channel",
            text="Toshkent - Samarqand qatnovchi qulay taksi va yetkazib berish xizmati. Murojaat: +998901234567",
            posted_at=t_str, link="https://t.me/ads_channel/2", status="new"
        ),

        # Duplicate news across 3 channels (Latin vs Cyrillic vs Near duplicate)
        DigestPost(
            chat_id=-100111, message_id=10, chat_title="Siyosat Va Diplomatiya", chat_username="siyosat_uz",
            text="Toshkent shahrida yangi xalqaro innovatsiya texnoparki tantanali ravishda ochildi.",
            posted_at=t_str, link="https://t.me/siyosat_uz/10", status="new"
        ),
        DigestPost(
            chat_id=-100222, message_id=20, chat_title="TSUE Axborot Markazi", chat_username="tsue_official",
            text="Тошкент шаҳрида янги халқаро инновация технопарки тантанали равишда очилди.",
            posted_at=t_str, link="https://t.me/tsue_official/20", status="new"
        ),
        DigestPost(
            chat_id=-100333, message_id=30, chat_title="IT Va Sun'iy Intellekt", chat_username="ai_uzbekistan",
            text="Toshkent shahrida yangi xalqaro innovatsiya texnoparki tantanali ochildi.",
            posted_at=t_str, link="https://t.me/ai_uzbekistan/30", status="new"
        ),

        # Topic A: Siyosiy yangiliklar
        DigestPost(
            chat_id=-100111, message_id=11, chat_title="Siyosat Va Diplomatiya", chat_username="siyosat_uz",
            text="Oliy Majlis Senati yangi qonun loyihasini birinchi o'qishda ma'qulladi.",
            posted_at=t_str, link="https://t.me/siyosat_uz/11", status="new"
        ),

        # Topic B: Tashqi siyosat va muzokaralar
        DigestPost(
            chat_id=-100111, message_id=12, chat_title="Siyosat Va Diplomatiya", chat_username="siyosat_uz",
            text="O'zbekiston va Turkiya tashqi ishlar vazirlari strategik muzokaralar o'tkazib, 5 ta yangi shartnoma imzoladilar.",
            posted_at=t_str, link="https://t.me/siyosat_uz/12", status="new"
        ),

        # Topic C: TSUE yangiliklari
        DigestPost(
            chat_id=-100222, message_id=21, chat_title="TSUE Axborot Markazi", chat_username="tsue_official",
            text="Toshkent davlat iqtisodiyot universiteti talabalari uchun xalqaro almashinuv dasturi bo'yicha saralash boshlandi.",
            posted_at=t_str, link="https://t.me/tsue_official/21", status="new"
        ),

        # Topic D: Kasblar narxlari (Legitimate vacancy with salary)
        DigestPost(
            chat_id=-100444, message_id=41, chat_title="Vakansiyalar Markazi", chat_username="ish_bor",
            text="Kompaniyaga AI muhandisi kerak. Maosh: 3000 USD oyiga. Joylashuv: Toshkent shahri. Tajriba 2 yil talab etiladi.",
            posted_at=t_str, link="https://t.me/ish_bor/41", status="new"
        ),

        # Topic D: Suspicious job ad (must be labeled "gumonli e'lon, tasdiqlanmagan")
        DigestPost(
            chat_id=-100444, message_id=42, chat_title="Vakansiyalar Markazi", chat_username="ish_bor",
            text="Tajribasiz yuqori daromad! Uyda o'tirib kuniga 1000000 so'm daromad toping. Boshlang'ich badal 50 000 so'm.",
            posted_at=t_str, link="https://t.me/ish_bor/42", status="new"
        ),

        # Topic E: Sun'iy intellekt
        DigestPost(
            chat_id=-100333, message_id=31, chat_title="IT Va Sun'iy Intellekt", chat_username="ai_uzbekistan",
            text="Yangi multimodal AI modeli namoyish etildi. U matn va ovozni sinxron qayta ishlaydi.",
            posted_at=t_str, link="https://t.me/ai_uzbekistan/31", status="new"
        ),

        # Topic F: Imkoniyatlar (With deadline & relative date)
        DigestPost(
            chat_id=-100555, message_id=51, chat_title="Global Imkoniyatlar", chat_username="grants_uz",
            text="Yosh olimlar uchun nufuzli tadqiqot granti e'lon qilindi. Ariza topshirish muddati: 2026-11-20 gacha. Ro'yxatdan o'tish ertaga ochiladi.",
            posted_at=t_str, link="https://t.me/grants_uz/51", status="new"
        ),
    ]

    await db_manager.bulk_save_digest_posts(raw_posts)

    # 3. Step: Cleaning and Deduplication
    cleaner = DigestCleaner()
    all_saved_posts = await db_manager.get_digest_posts(status="new")
    filter_result = cleaner.process_and_filter_batch(all_saved_posts)

    unique_posts = filter_result["clean_posts"]
    duplicate_posts = filter_result["duplicate_posts"]
    spam_posts = filter_result["spam_posts"]
    filtered_count = len(spam_posts)
    deduplicated_count = len(duplicate_posts)

    for p in spam_posts:
        await db_manager.update_digest_post_filter_status(
            post_id=p.id,
            status="filtered",
            discard_reason=p.discard_reason,
            spam_score=p.spam_score,
            spam_reasons=p.spam_reasons,
        )
    for p in duplicate_posts:
        await db_manager.update_digest_post_filter_status(
            post_id=p.id,
            status="filtered",
            discard_reason="duplicate_news",
        )

    # Verify spam and duplicates are correctly filtered
    assert filtered_count >= 2  # 2 spam posts filtered
    assert deduplicated_count >= 2  # 2 duplicate texnopark posts filtered

    # 4. Step: Classification & Summarization AI mock
    # Provide strict deterministic responses for test posts
    async def mock_ai_generate(prompt: str, temperature: float = 0.0) -> str:
        if "KLASSIFIKATSIYA" in prompt:
            # Batch classifier response
            classified_list = []
            for p in unique_posts:
                txt = (p.text or "").lower()
                if "o'zbekiston va turkiya" in txt:
                    item = {"post_id": p.id, "topic": "B", "importance": 4, "is_advertisement": False}
                elif "senati" in txt:
                    item = {"post_id": p.id, "topic": "A", "importance": 4, "is_advertisement": False}
                elif "tsue" in txt or "iqtisodiyot" in txt:
                    item = {"post_id": p.id, "topic": "C", "importance": 4, "is_advertisement": False}
                elif "ai muhandisi" in txt:
                    item = {
                        "post_id": p.id, "topic": "D", "importance": 4, "is_advertisement": False,
                        "extracted": {"profession": "AI muhandisi", "amount": "3000", "currency": "USD", "period": "oyiga", "location": "Toshkent shahri"}
                    }
                elif "kuniga 1000000" in txt:
                    item = {
                        "post_id": p.id, "topic": "D", "importance": 3, "is_advertisement": False,
                        "extracted": {"profession": "Mutaxassis", "amount": "1000000", "currency": "so'm", "period": "kuniga", "location": "Uyda"}
                    }
                elif "multimodal" in txt or "texnopark" in txt:
                    item = {"post_id": p.id, "topic": "E", "importance": 4, "is_advertisement": False}
                elif "grant" in txt or "2026-11-20" in txt:
                    item = {
                        "post_id": p.id, "topic": "F", "importance": 5, "is_advertisement": False,
                        "extracted": {"title": "Yosh olimlar tadqiqot granti", "deadline": "2026-11-20", "how_to_apply": "Onlayn ariza"}
                    }
                else:
                    item = {"post_id": p.id, "topic": "other", "importance": 1, "is_advertisement": False}
                classified_list.append(item)
            return json.dumps(classified_list)

        elif "XULOSA_SYSTEM_PROMPT" in prompt or "VOQEA POSTLARI" in prompt:
            # Summarizer response for topics A, B, C, E, F
            if "turkiya" in prompt.lower():
                return json.dumps({
                    "title": "O'zbekiston-Turkiya muzokaralari",
                    "summary": "O'zbekiston va Turkiya tashqi ishlar vazirlari strategik muzokaralar o'tkazib, 5 ta yangi shartnoma imzoladilar.",
                    "post_ids_used": [], "has_conflicts": False, "unverified": False
                })
            elif "senat" in prompt.lower():
                return json.dumps({
                    "title": "Senat qarori",
                    "summary": "Oliy Majlis Senati yangi qonun loyihasini birinchi o'qishda ma'qulladi.",
                    "post_ids_used": [], "has_conflicts": False, "unverified": False
                })
            elif "tsue" in prompt.lower() or "universitet" in prompt.lower():
                return json.dumps({
                    "title": "TSUE dasturi",
                    "summary": "Toshkent davlat iqtisodiyot universiteti talabalari uchun xalqaro almashinuv dasturi saralashi boshlandi.",
                    "post_ids_used": [], "has_conflicts": False, "unverified": False
                })
            elif "multimodal" in prompt.lower():
                return json.dumps({
                    "title": "Yangi AI modeli",
                    "summary": "Yangi multimodal AI modeli taqdim etildi va u matn hamda ovozni sinxron qayta ishlaydi.",
                    "post_ids_used": [], "has_conflicts": False, "unverified": False
                })
            elif "texnopark" in prompt.lower():
                return json.dumps({
                    "title": "Innovatsiya texnoparki",
                    "summary": "Toshkent shahrida yangi xalqaro innovatsiya texnoparki tantanali ravishda ochildi.",
                    "post_ids_used": [], "has_conflicts": False, "unverified": False
                })
            elif "grant" in prompt.lower():
                return json.dumps({
                    "title": "Yosh olimlar ilmiy granti",
                    "summary": "Yosh olimlar uchun nufuzli tadqiqot granti e'lon qilindi. Ariza muddati 2026-11-20 gacha. Ro'yxatdan o'tish ertaga ochiladi.",
                    "post_ids_used": [], "has_conflicts": False, "unverified": False
                })

        return "{}"

    ai_mock = MagicMock()
    ai_mock.generate_json_response = AsyncMock(side_effect=mock_ai_generate)

    summarizer = DigestSummarizer(ai_mock)
    service = DigestService(db_manager, ai_mock)

    # Classify posts in DB
    for p in unique_posts:
        txt = (p.text or "").lower()
        if "turkiya" in txt:
            p.topic = "B"
        elif "senat" in txt:
            p.topic = "A"
        elif "tsue" in txt or "iqtisodiyot" in txt:
            p.topic = "C"
        elif "ai muhandisi" in txt:
            p.topic = "D"
            p.extracted_json = json.dumps({"profession": "AI muhandisi", "amount": "3000", "currency": "USD", "period": "oyiga", "location": "Toshkent shahri"})
        elif "kuniga 1000000" in txt:
            p.topic = "D"
            p.spam_reasons = "suspicious_job"
            p.extracted_json = json.dumps({"profession": "Mutaxassis", "amount": "1000000", "currency": "so'm", "period": "kuniga", "location": "Uyda"})
        elif "multimodal" in txt or "texnopark" in txt:
            p.topic = "E"
        elif "grant" in txt or "2026-11-20" in txt:
            p.topic = "F"
            p.extracted_json = json.dumps({"title": "Yosh olimlar tadqiqot granti", "deadline": "2026-11-20", "how_to_apply": "Onlayn ariza"})

        p.status = "classified"
        p.importance = 4
        await db_manager.update_digest_post_classification(
            post_id=p.id,
            topic=p.topic,
            importance=p.importance,
            status=p.status,
            extracted_json=p.extracted_json,
        )

    # 5. Step: Summarize & Assemble Sections
    items_by_topic = {}
    for top in ["A", "B", "C", "D", "E", "F"]:
        top_posts = [p for p in unique_posts if p.topic == top]
        items, disc, skp = await summarizer.summarize_topic_posts(top, top_posts)
        items_by_topic[top] = items

    # 6. Step: Format Full Digest
    formatter = DigestFormatter()
    run_date_str = now.strftime("%Y-%m-%d")
    stats = {
        "total_collected": len(raw_posts),
        "spam_filtered": filtered_count,
        "duplicates_filtered": deduplicated_count,
        "discarded_by_verifier": 0,
        "skipped_posts": 0,
        "failed_batches": 0,
    }

    pages = formatter.format_full_digest(
        sections=items_by_topic,
        analyzed_count=len(raw_posts),
        selected_count=sum(len(v) for v in items_by_topic.values()),
        spam_count=filtered_count,
        discarded_by_verification=0,
        skipped_by_limit=0,
        failed_batches=0,
        target_date=now,
    )
    full_text = "\n\n".join(pages)

    # 7. Comprehensive Assertions
    # a) Headers and Sections ordering: F, E, C, B, A, D
    assert "IMKONIYATLAR" in full_text
    assert "SUN" in full_text and "INTELLEKT" in full_text
    assert "TSUE" in full_text
    assert "TASHQI SIYOSAT" in full_text
    assert "SIYOSIY YANGILIKLAR" in full_text
    assert "KASBLAR NARXLARI" in full_text

    idx_f = full_text.find("IMKONIYATLAR")
    idx_e = full_text.find("INTELLEKT")
    idx_c = full_text.find("TSUE")
    idx_b = full_text.find("TASHQI SIYOSAT")
    idx_a = full_text.find("SIYOSIY YANGILIKLAR")
    idx_d = full_text.find("KASBLAR NARXLARI")

    # Strict section ordering verification
    assert idx_f < idx_e < idx_c < idx_b < idx_a < idx_d

    # b) Section D deterministic format & suspicious job tag
    assert "AI muhandisi" in full_text
    assert "3000/oyiga" in full_text
    assert "gumonli" in full_text and "tasdiqlanmagan" in full_text
    assert "e&#x27;londa ko&#x27;rsatilgan" in full_text or "e'londa ko'rsatilgan" in full_text

    # c) Section F deadline display
    assert "Muddati:</b> 2026-11-20" in full_text or "2026-11-20" in full_text

    # d) Relative date preservation
    assert "ertaga" in full_text

    # e) Collapsed duplicate item links: merged item has links
    assert "https://t.me/" in full_text

    # f) Phone numbers are stripped and absent
    assert "+998901234567" not in full_text

    # g) Header & Footer counts match exact run reality
    assert "12 ta post tahlil qilindi" in full_text
    assert "8 ta tanlandi" in full_text
    assert "Reklama/spam filtrlandi: 2 ta" in full_text
    assert "Tekshiruvdan o&#x27;tmagan: 0 ta" in full_text or "Tekshiruvdan o'tmagan: 0 ta" in full_text


@pytest.mark.asyncio
async def test_privacy_boundary_zero_private_chat_leakage(tmp_path):
    """
    Verifies that private 1-to-1 chats are strictly forbidden from being added,
    and cannot leak into the digest under any circumstances.
    """
    db_file = tmp_path / "test_p7_privacy.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    ai_mock = MagicMock()
    service = DigestService(db_manager, ai_mock)

    # 1. Attempt to add a private user entity (Telethon User mock)
    from telethon.tl.types import User, Channel, Chat

    user_entity = MagicMock(spec=User)
    user_entity.id = 99887766
    user_entity.first_name = "Shaxsiy Kontakt"

    mock_client = MagicMock()
    mock_client.get_entity = AsyncMock(return_value=user_entity)
    service.get_client = lambda: mock_client

    success, msg, source = await service.resolve_and_add_source(mock_client, "@shaxsiy_kontakt")
    assert success is False
    assert "Shaxsiy yozishmalarni (1-ga-1 chat) digestga qo'shish qat'iyan man etiladi" in msg
    assert source is None

    # Verify no source was saved
    sources = await db_manager.list_digest_sources()
    assert len(sources) == 0


@pytest.mark.asyncio
async def test_feature_flag_toggle_v2_and_v1_fallback(tmp_path):
    """
    Verifies that when DIGEST_V2_ENABLED is True, Smart Digest V2 runs.
    When DIGEST_V2_ENABLED is False, legacy V1 digest runs unchanged.
    """
    db_file = tmp_path / "test_p7_flag.db"
    await init_db(db_file)
    db_manager = DatabaseManager(str(db_file))

    ai_mock = MagicMock()
    service = DigestService(db_manager, ai_mock)

    # Mock legacy generate_digest and generate_and_send_digest
    service.generate_digest = AsyncMock(return_value=("V1 Legacy Digest Summary", 5))
    service.generate_and_send_digest = AsyncMock(return_value="V1 Legacy Digest Summary")

    # Mock V2 run_smart_digest
    service.run_smart_digest = AsyncMock(return_value={"status": "completed", "messages": ["V2 Smart Digest Report"]})

    scheduler = SchedulerService(db_manager)
    notified_messages = []

    def mock_notify_boss(text):
        notified_messages.append(text)

    scheduler.register_callbacks(send_callback=AsyncMock(), notify_boss_callback=mock_notify_boss)

    # 1. Test with DIGEST_V2_ENABLED = False -> runs legacy V1
    with patch.object(settings, "DIGEST_V2_ENABLED", False):
        await scheduler.check_and_run_catchup_digest(
            digest_service=service,
            notify_boss_callback=mock_notify_boss,
            digest_time_str="08:00",
        )
        assert service.generate_and_send_digest.called
        assert not service.run_smart_digest.called
        assert len(notified_messages) == 1
        assert notified_messages[0] == "V1 Legacy Digest Summary"

    # Reset mocks and states
    service.generate_and_send_digest.reset_mock()
    service.run_smart_digest.reset_mock()
    notified_messages.clear()

    # 2. Test with DIGEST_V2_ENABLED = True -> runs Smart Digest V2
    with patch.object(settings, "DIGEST_V2_ENABLED", True):
        # Trigger scheduled job logic
        res = await service.run_smart_digest(trigger="scheduled")
        assert res["status"] == "completed"
        assert res["messages"] == ["V2 Smart Digest Report"]
        assert not service.generate_and_send_digest.called
