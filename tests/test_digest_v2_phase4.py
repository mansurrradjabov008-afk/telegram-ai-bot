"""
Unit tests for Smart Digest V2 - Phase 4: Summarization & Anti-Hallucination Code Verification.
Tests event grouping, AI summarization, code verification (numbers normalization, URL stripping,
phone stripping, entity verification, post_id check), deterministic Section D,
Section F deadline sorting, topic limits, and topic failure isolation.
"""

import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config import settings
from database.connection import init_db
from database.crud import DatabaseManager
from database.models import DigestPost
from services.ai_engine import AIEngine
from services.digest_verifier import DigestVerifier, NumberNormalizer
from services.digest_summarizer import DigestSummarizer, DigestItem, AISummaryResponse
from services.digest_service import DigestService


@pytest_asyncio.fixture
async def temp_db_crud(tmp_path):
    """Initializes isolated test database."""
    test_db = tmp_path / "test_phase4.db"
    await init_db(test_db)
    crud = DatabaseManager(test_db)
    return crud, test_db


# ==================== 1. NUMBER NORMALIZATION & VERIFICATION (RULE 3b) ====================

def test_number_normalizer_thousand_separators():
    """Verifies thousand separators '300 000', '300,000', '300.000' normalize to 300000."""
    n1 = NumberNormalizer.extract_numbers("Jami 300 000 so'm ajratildi")
    assert "300000" in n1

    n2 = NumberNormalizer.extract_numbers("Narxi 300,000 so'm")
    assert "300000" in n2

    n3 = NumberNormalizer.extract_numbers("Qiymati 300.000 so'm")
    assert "300000" in n3


def test_number_normalizer_multipliers():
    """Verifies magnitude suffixes '300 ming', '300 тыс', '300k' match 300000."""
    assert "300000" in NumberNormalizer.extract_numbers("300 ming so'm")
    assert "300000" in NumberNormalizer.extract_numbers("300 тыс рублей")
    assert "300000" in NumberNormalizer.extract_numbers("Maosh 300k")
    assert "5000000" in NumberNormalizer.extract_numbers("5 mln dollar")
    assert "1500000" in NumberNormalizer.extract_numbers("1.5 million so'm")


def test_verifier_numbers_present_in_source_passes():
    """Verifies that numbers present in cited source pass verification."""
    verifier = DigestVerifier()
    source_post = DigestPost(id=1, text="Universitet 300 ming so'm stipendiya beradi.")
    summary = "Talabalar uchun 300 000 so'm stipendiya ajratildi."

    num_ok, err = verifier.verify_numbers(summary, [source_post])
    assert num_ok is True
    assert err is None


def test_verifier_invented_number_fails_verification():
    """Verifies that an invented number not in the source post causes discard."""
    verifier = DigestVerifier()
    source_post = DigestPost(id=1, text="Universitet stipendiya beradi.")
    summary = "Talabalar uchun 500 000 so'm stipendiya ajratildi."  # 500 000 is invented!

    num_ok, err = verifier.verify_numbers(summary, [source_post])
    assert num_ok is False
    assert "unverified_number" in err


# ==================== 2. URL & PHONE STRIPPING (RULES 3c, 3d) ====================

def test_verifier_url_stripping():
    """Verifies unapproved external URLs are stripped while source URLs are kept."""
    verifier = DigestVerifier()
    approved = {"https://t.me/rasmiy_kanal/123"}
    summary = "Yangilik e'lon qilindi: https://t.me/rasmiy_kanal/123 va firibgar sayt: https://scam-site.org/bonus"

    cleaned = verifier.strip_unapproved_urls(summary, approved)
    assert "https://t.me/rasmiy_kanal/123" in cleaned
    assert "https://scam-site.org/bonus" not in cleaned


def test_verifier_phone_number_stripping():
    """Verifies phone numbers are stripped from summary text."""
    verifier = DigestVerifier()
    summary = "Bog'lanish uchun raqam: +998 90 123 45 67 yoki 998901234567 ga murojaat qiling."
    cleaned = verifier.strip_phone_numbers(summary)

    assert "+998" not in cleaned
    assert "123 45 67" not in cleaned


# ==================== 3. PROPER ENTITY VERIFICATION (RULE 3e) ====================

def test_verifier_proper_entity_with_cyrillic_transliteration():
    """Verifies that entity in Uzbek Cyrillic in source matches Latin in summary."""
    verifier = DigestVerifier()
    source_post = DigestPost(
        id=1,
        chat_title="Yangiliklar",
        text="Шавкат Мирзиёев Самарқанд вилоятига ташриф буюрди va ТДИУ rektori bilan uchrashdi.",
    )
    summary = "Shavkat Mirziyoyev Samarqand viloyatiga bordi."

    ent_ok, err = verifier.verify_proper_entities(summary, [source_post])
    assert ent_ok is True
    assert err is None


def test_verifier_hallucinated_entity_fails():
    """Verifies that hallucinated person or organization fails verification."""
    verifier = DigestVerifier()
    source_post = DigestPost(id=1, text="Toshkentda yangi texnopark ochildi.")
    summary = "Elon Musk Toshkent texnoparkiga tashrif buyurdi."  # Elon Musk not in source!

    ent_ok, err = verifier.verify_proper_entities(summary, [source_post])
    assert ent_ok is False
    assert "unverified_entity" in err


# ==================== 4. POST ID & FULL VERIFICATION (RULE 3a, NO AI FIX) ====================

def test_verifier_missing_post_id_discarded():
    """Verifies that referencing a non-existent post_id causes discard."""
    verifier = DigestVerifier()
    source_post = DigestPost(id=10, text="AI texnologiyalari rivojlanmoqda.")

    is_valid, _, reason = verifier.verify_item(
        summary_text="AI rivojlanmoqda.",
        source_posts=[source_post],
        post_ids_used=[99],  # ID 99 does not exist!
    )
    assert is_valid is False
    assert "referenced_post_id_missing" in reason


# ==================== 5. SECTION D DETERMINISTIC BUILDING ====================

def test_build_section_d_items_deterministic():
    """Verifies Section D is built directly from extracted fields with appropriate labels."""
    ai_mock = MagicMock(spec=AIEngine)
    summarizer = DigestSummarizer(ai_mock)

    posts = [
        DigestPost(
            id=1,
            chat_title="IT Jobs",
            link="https://t.me/itjobs/10",
            posted_at="2026-10-09",
            extracted_json=json.dumps({
                "profession": "Backend Developer",
                "amount": "$2500",
                "period": "oyiga",
                "location": "Toshkent",
            }),
        ),
        DigestPost(
            id=2,
            chat_title="Fast Money",
            link="https://t.me/fast/20",
            posted_at="2026-10-09",
            spam_reasons="suspicious_job: kuniga $1000",
            extracted_json=json.dumps({
                "profession": "Operator",
                "amount": "$1000",
                "period": "kuniga",
                "location": "Uyda",
            }),
        ),
    ]

    items = summarizer.build_section_d_items(posts)
    assert len(items) == 2

    # Normal vacancy
    assert items[0].title == "Backend Developer"
    assert "e'londa ko'rsatilgan, tasdiqlanmagan" in items[0].labels[0]
    assert "$2500/oyiga" in items[0].summary

    # Suspicious vacancy
    assert items[1].title == "Operator"
    assert "gumonli e'lon, tasdiqlanmagan" in items[1].labels[0]
    assert items[1].is_suspicious_job is True


# ==================== 6. SECTION F DEADLINE SORTING ====================

def test_sort_section_f_items_by_deadline():
    """Verifies Section F items are sorted by nearest deadline first."""
    ai_mock = MagicMock(spec=AIEngine)
    summarizer = DigestSummarizer(ai_mock)

    item1 = DigestItem(topic="F", title="Grant C", summary="", source_links=[], post_date="2026-10-09", deadline="ko'rsatilmagan")
    item2 = DigestItem(topic="F", title="Grant A", summary="", source_links=[], post_date="2026-10-09", deadline="2026-10-15")
    item3 = DigestItem(topic="F", title="Grant B", summary="", source_links=[], post_date="2026-10-09", deadline="2026-10-20")

    sorted_f = summarizer.sort_section_f_items([item1, item3, item2])
    assert sorted_f[0].title == "Grant A"
    assert sorted_f[1].title == "Grant B"
    assert sorted_f[2].title == "Grant C"


# ==================== 7. TOPIC FAILURE ISOLATION ====================

@pytest.mark.asyncio
async def test_topic_failure_isolation():
    """Verifies that if one topic fails, other topics are still processed and failed topic recorded."""
    ai_mock = MagicMock(spec=AIEngine)
    summarizer = DigestSummarizer(ai_mock)

    posts = [
        DigestPost(id=1, topic="E", importance=4, text="AI yangilik 1", chat_title="AI Channel"),
        DigestPost(id=2, topic="A", importance=4, text="Siyosat yangilik 1", chat_title="Polit Channel"),
    ]

    # Make summarize_topic_posts fail for topic 'E' only
    orig_summarize = summarizer.summarize_topic_posts

    async def patched_summarize(topic, topic_posts):
        if topic == "E":
            raise RuntimeError("Gemini API connection error on Topic E")
        return await orig_summarize(topic, topic_posts)

    with patch.object(summarizer, "summarize_topic_posts", side_effect=patched_summarize):
        res = await summarizer.process_all_topics(posts)

    assert "E" in res["failed_topics"]
    assert len(res["sections"]["E"]) == 0
    # Other topics processed without throwing exception
    assert "A" not in res["failed_topics"]


# ==================== 8. END-TO-END PIPELINE INTEGRATION ====================

@pytest.mark.asyncio
async def test_digest_service_summarization_pipeline(temp_db_crud):
    """End-to-end integration test of summarize_run_posts with database persistence."""
    crud, _ = temp_db_crud
    ai_mock = MagicMock(spec=AIEngine)

    service = DigestService(crud, ai_mock)

    # Insert classified post into DB
    p = DigestPost(
        chat_id=-1001,
        message_id=50,
        chat_title="AI News",
        text="Google yangi Gemini 3.5 modelini chiqardi.",
        topic="E",
        importance=5,
        status="classified",
    )
    p_id = await crud.save_digest_post(p)

    ai_resp = json.dumps({
        "title": "Gemini 3.5 Taqdimoti",
        "summary": "Google yangi Gemini 3.5 modelini chiqardi.",
        "post_ids_used": [p_id],
        "unverified": False,
        "has_conflicts": False,
    })
    ai_mock.generate_json_response = AsyncMock(return_value=ai_resp)

    res = await service.summarize_run_posts()
    assert res["total_items_selected"] == 1
    assert len(res["sections"]["E"]) == 1
    assert res["sections"]["E"][0].title == "Gemini 3.5 Taqdimoti"

    # Verify post status updated to 'included' in database
    db_posts = await crud.get_digest_posts(status="included")
    assert len(db_posts) == 1
    assert db_posts[0].id == p_id
