"""
Unit tests for Smart Digest V2 - Phase 2: Cleaning, Normalization, Deduplication and Spam Filtering.
Pure code, deterministic, without AI calls.
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest
import pytest_asyncio

from config import settings
from database.connection import init_db
from database.crud import DatabaseManager
from database.models import DigestPost
from services.digest_cleaner import (
    transliterate_uzbek_cyrillic_to_latin,
    normalize_text_for_comparison,
    compute_normalized_hash,
    calculate_text_similarity,
    DigestCleaner,
)
from services.digest_service import DigestService


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    db_file = tmp_path / "test_digest_phase2.db"
    await init_db(db_file)
    manager = DatabaseManager(db_path=db_file)
    return manager


@pytest_asyncio.fixture
def cleaner():
    return DigestCleaner()


# ==================== 1. TRANSLITERATION & NORMALIZATION ====================

def test_transliterate_uzbek_cyrillic_to_latin():
    """Verifies Uzbek Cyrillic to Latin transliteration for digraphs and single characters."""
    cyr = "Шошилинч: Ўзбекистонда сунъий интеллект бўйича янги қонун қабул қилинди."
    lat = transliterate_uzbek_cyrillic_to_latin(cyr)
    assert "Shoshilinch" in lat
    assert "O'zbekistonda" in lat
    assert "sun'iy" in lat
    assert "bo'yicha" in lat
    assert "yangi" in lat
    assert "qonun" in lat
    assert "qabul" in lat


def test_cross_script_normalization_equality():
    """
    Guarantees that the exact same news published in Uzbek Cyrillic and Uzbek Latin
    produces the identical normalized text and hash.
    """
    text_latin = "Toshkent davlat iqtisodiyot universitetida yangi grant e'lon qilindi! Batafsil: https://tsue.uz/grant"
    text_cyrillic = "Тошкент давлат иқтисодиёт университетида янги грант эълон қилинди! Батафсил: https://tsue.uz/grant"

    norm_latin = normalize_text_for_comparison(text_latin)
    norm_cyrillic = normalize_text_for_comparison(text_cyrillic)

    assert norm_latin == norm_cyrillic
    assert compute_normalized_hash(text_latin) == compute_normalized_hash(text_cyrillic)


def test_normalize_strips_emojis_urls_and_whitespace():
    """Verifies that URLs, emojis, and irregular spacing are stripped cleanly."""
    dirty = "  🔥 DIQQAT! 🚀 Yangi startap tanlovi https://t.me/channel/123   ro'yxatdan o'ting!   "
    clean = normalize_text_for_comparison(dirty)
    assert "diqqat yangi startap tanlovi ro'yxatdan o'ting" == clean
    assert "https" not in clean
    assert "🔥" not in clean


# ==================== 2. DEDUPLICATION (EXACT & NEAR-DUPLICATE) ====================

def test_exact_hash_deduplication(cleaner: DigestCleaner):
    """
    Exact duplicates across different channels are deduplicated:
    Keeps 1 item and merges source references.
    """
    post1 = DigestPost(
        id=1,
        chat_id=-1001,
        message_id=10,
        chat_title="Kanal A",
        text="Toshkentda 10-oktyabr kuni xalqaro sun'iy intellekt forumi ochiladi.",
        link="https://t.me/kanal_a/10",
    )
    post2 = DigestPost(
        id=2,
        chat_id=-1002,
        message_id=20,
        chat_title="Kanal B",
        text="Тошкентда 10-октябрь куни халқаро сунъий интеллект форуми очилади.",
        link="https://t.me/kanal_b/20",
    )

    unique, duplicates = cleaner.deduplicate_posts([post1, post2])

    assert len(unique) == 1
    assert len(duplicates) == 1
    assert unique[0].id == 1
    assert duplicates[0].id == 2
    assert duplicates[0].status == "filtered"
    assert "duplicate" in duplicates[0].discard_reason
    # Verify merged source link
    assert "https://t.me/kanal_b/20" in (unique[0].original_source or "")


def test_near_duplicate_detection(cleaner: DigestCleaner):
    """
    Near-duplicates with promotional tails or slight additions are detected and merged.
    """
    post1 = DigestPost(
        id=1,
        chat_id=-1001,
        message_id=101,
        chat_title="Kanal 1",
        text="Prezident Shavkat Mirziyoyev raisligida raqamlashtirish va IT sohasini rivojlantirish bo'yicha yig'ilish o'tkazildi.",
        link="https://t.me/k1/101",
    )
    post2 = DigestPost(
        id=2,
        chat_id=-1002,
        message_id=202,
        chat_title="Kanal 2",
        text="Prezident Shavkat Mirziyoyev raisligida raqamlashtirish va IT sohasini rivojlantirish bo'yicha yig'ilish o'tkazildi. Batafsil bizning kanalda.",
        link="https://t.me/k2/202",
    )

    sim = calculate_text_similarity(post1.text, post2.text)
    assert sim >= 0.85

    unique, duplicates = cleaner.deduplicate_posts([post1, post2])
    assert len(unique) == 1
    assert len(duplicates) == 1
    assert duplicates[0].status == "filtered"


# ==================== 3. SPAM, SCAM AND AD FILTERING ====================

def test_filter_get_rich_quick_scam(cleaner: DigestCleaner):
    """Get-rich-quick claims are flagged as spam."""
    post = DigestPost(
        chat_id=-1001,
        message_id=1,
        chat_title="Spam Kanal",
        text="Kafolatlangan daromad! Tez pul topish siri. Kuniga 500 000 so'm daromad olishni xohlaysizmi? Havolaga bosing!",
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_spam is True
    assert score >= 0.60
    assert any("tez boyib ketish" in r for r in reasons)


def test_filter_crypto_doubling(cleaner: DigestCleaner):
    """Crypto doubling / piramida promises are flagged as spam."""
    post = DigestPost(
        chat_id=-1001,
        message_id=2,
        chat_title="Crypto Scam",
        text="Investitsiya qiling va pulni 2 barobar qilib oling! Daromad 100% kafolatlangan.",
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_spam is True
    assert any("kripto" in r for r in reasons)


def test_filter_giveaway_and_lottery(cleaner: DigestCleaner):
    """Giveaways and lotteries are flagged as spam."""
    post = DigestPost(
        chat_id=-1001,
        message_id=3,
        chat_title="Giveaway",
        text="Shoshiling! Yangi iPhone yutib oling! Yutuqli o'yin boshlandi, obuna bo'ling va yuting!",
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_spam is True
    assert any("yutuqli o'yin" in r for r in reasons)


def test_filter_goods_and_services_ad(cleaner: DigestCleaner):
    """Selling goods, taxi, and delivery advertisements are filtered."""
    post = DigestPost(
        chat_id=-1001,
        message_id=4,
        chat_title="Bozor",
        text="Arzon narxlarda sotiladi! Bepul yetkazib berish xizmati mavjud. Katta chegirma va skidka do'konimizda!",
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_spam is True
    assert any("tovar / xizmat" in r for r in reasons)


def test_filter_referral_invite_spam(cleaner: DigestCleaner):
    """Referral and invite link spam is filtered."""
    post = DigestPost(
        chat_id=-1001,
        message_id=5,
        chat_title="Spam Bot",
        text="Do'stlaringizni taklif qiling va har bir do'stingiz uchun 10 000 so'm oling! Referal havola orqali kiring.",
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_spam is True
    assert any("referal" in r for r in reasons)


# ==================== 4. LEGITIMATE OPPORTUNITY SAFELIST PROTECTION ====================

def test_legitimate_job_vacancy_is_not_spam(cleaner: DigestCleaner):
    """
    Legitimate job vacancies with salary and contact numbers must NEVER be filtered as spam.
    """
    post = DigestPost(
        chat_id=-1001,
        message_id=10,
        chat_title="IT Jobs",
        text=(
            "Vakansiya: Senior Python Developer\n"
            "Talablar: Python, FastAPI, Docker, SQLite.\n"
            "Majburiyatlar: Tizim arxitekturasini ishlab chiqish.\n"
            "Maosh: 2500$ - 3500$\n"
            "Bog'lanish uchun rezyume yuboring: +998901234567 yoki @hr_company"
        ),
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_spam is False
    assert is_suspicious is False
    assert score < 0.60


def test_legitimate_tsue_grant_is_not_spam(cleaner: DigestCleaner):
    """
    TSUE grant opportunities and academic deadlines must NEVER be filtered as spam.
    """
    post = DigestPost(
        chat_id=-1001,
        message_id=11,
        chat_title="TSUE Rasmiy",
        text=(
            "Toshkent davlat iqtisodiyot universiteti iqtidorli talabalar uchun yangi grant e'lon qiladi!\n"
            "Stipendiya miqdori: oyiga 2 000 000 so'm.\n"
            "Hujjat topshirish muddati (deadline): 15-noyabr.\n"
            "Batafsil ma'lumot dekanatda: +998712001122"
        ),
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_spam is False
    assert is_suspicious is False
    assert score < 0.60


def test_legitimate_ai_course_is_not_spam(cleaner: DigestCleaner):
    """Educational AI courses and competitions must be preserved."""
    post = DigestPost(
        chat_id=-1001,
        message_id=12,
        chat_title="AI Club",
        text=(
            "Sun'iy intellekt va Machine Learning bo'yicha intensiv kurs ochilmoqda.\n"
            "Dastur: Deep Learning, Transformers, NLP.\n"
            "Ariza topshirish oxirgi muddati: 1-dekabr.\n"
            "Eng yaxshi loyihalar uchun maxsus grant beriladi."
        ),
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_spam is False


# ==================== 5. SUSPICIOUS JOB ADS FLAGGING ====================

def test_suspicious_job_ad_flagged(cleaner: DigestCleaner):
    """
    Suspicious job ads (unrealistic earnings, 'no experience high income', upfront fees)
    are flagged with is_suspicious_job=True, NOT discarded as trash spam.
    """
    post = DigestPost(
        chat_id=-1001,
        message_id=20,
        chat_title="Vakansiyalar",
        text=(
            "Ishga taklif: Uyda o'tirib kuniga 1-2 soat ishlab oyiga 3000$ topish mumkin!\n"
            "Tajriba talab qilinmaydi, yuqori daromad kafolatlangan.\n"
            "Boshlash uchun 50$ boshlang'ich badal to'lang."
        ),
    )
    score, reasons, is_spam, is_suspicious = cleaner.score_post(post)
    assert is_suspicious is True
    # It is NOT dropped as silent spam; it is kept with suspicious flag
    assert is_spam is False
    assert any("gumonli e'lon" in r for r in reasons)


# ==================== 6. BATCH PROCESSING & DATABASE PIPELINE ====================

@pytest.mark.asyncio
async def test_digest_service_clean_and_filter_pipeline(temp_db: DatabaseManager):
    """
    Verifies that DigestService.clean_and_filter_run_posts:
    1. Reads posts from DB.
    2. Runs deduplication and spam filtering.
    3. Updates database status ('filtered' vs 'new') and reasons.
    4. Accurately reports statistics.
    """
    service = DigestService(db_manager=temp_db, ai_engine=None)

    # Insert 4 posts: 1 clean news, 1 duplicate of news, 1 spam ad, 1 clean vacancy
    p1 = DigestPost(
        chat_id=-1001,
        message_id=1,
        chat_title="Yangiliklar",
        text="O'zbekiston va Qozog'iston o'rtasida yangi savdo shartnomasi imzolandi.",
        digest_run_id=10,
    )
    p2 = DigestPost(
        chat_id=-1002,
        message_id=2,
        chat_title="Xabarlar",
        text="Ўзбекистон ва Қозоғистон ўртасида янги савдо шартномаси имзоланди.",  # Cyrillic duplicate of p1
        digest_run_id=10,
    )
    p3 = DigestPost(
        chat_id=-1003,
        message_id=3,
        chat_title="Bozor",
        text="Katta chegirma! Bepul yetkazib berish xizmati bilan arzon narxlarda sotiladi. Skidka 50%!",
        digest_run_id=10,
    )
    p4 = DigestPost(
        chat_id=-1004,
        message_id=4,
        chat_title="TSUE",
        text="TSUE talabalari uchun xalqaro grant tanlovi e'lon qilindi. Hujjat topshirish muddati: 20-noyabr.",
        digest_run_id=10,
    )

    await temp_db.bulk_save_digest_posts([p1, p2, p3, p4])

    stats = await service.clean_and_filter_run_posts(run_id=10)

    assert stats["total_input"] == 4
    assert stats["duplicates_count"] == 1
    assert stats["spam_count"] == 1
    assert stats["clean_remaining"] == 2

    # Verify database updates
    all_posts = await temp_db.get_digest_posts(run_id=10, limit=10)
    status_map = {p.message_id: p.status for p in all_posts}

    assert status_map[1] == "new"       # Surviving primary news
    assert status_map[2] == "filtered"  # Duplicate
    assert status_map[3] == "filtered"  # Spam ad
    assert status_map[4] == "new"       # Legitimate TSUE grant

    # Verify count_filtered_spam_posts
    filtered_cnt = await temp_db.count_filtered_spam_posts(run_id=10)
    assert filtered_cnt == 2
