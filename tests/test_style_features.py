"""
Unit tests for Writing Style Statistics and Profile Engine (D1, D3, D4, Phase 3).
"""

from collections import Counter
import pytest
from services.style_features import (
    compute_style_statistics,
    extract_candidate_phrases,
    filter_and_rank_phrases,
    build_style_profile,
    StyleStatistics,
)


def test_compute_statistics_exact_ground_truth():
    # Synthetic corpus:
    # 10 messages of "Ha." (length 3, 1 word, 0 emoji, none exclamation)
    # 10 messages of "Rahmat katta! 👍" (length 15, 3 words, 1 emoji, single exclamation)
    samples = []
    for _ in range(10):
        samples.append({
            "contact_user_id": 1,
            "text_redacted": "Ha.",
            "char_len": 3,
            "token_len": 1,
            "lang": "uz-lat",
            "register": None,
        })
    for _ in range(10):
        samples.append({
            "contact_user_id": 2,
            "text_redacted": "Rahmat katta! 👍",
            "char_len": 15,
            "token_len": 3,
            "lang": "uz-lat",
            "register": "siz",
        })

    stats = compute_style_statistics(samples)
    assert stats.total_samples == 20
    assert stats.avg_chars == (10 * 3 + 10 * 15) / 20  # 9.0
    assert stats.min_chars == 3
    assert stats.max_chars == 15
    assert stats.avg_words == (10 * 1 + 10 * 3) / 20  # 2.0
    assert stats.single_excl_rate == 0.5  # 10 out of 20
    assert stats.multi_excl_rate == 0.0
    assert stats.avg_emojis == 0.5  # 10 emojis in 20 messages
    assert stats.top_emojis[0][0] == "👍"


def test_phrase_scrubbing_drops_entities_urls_digits():
    forbidden = {"mansur", "ali", "valiyev", "uzcard"}

    # 1. Clean opener
    extracted = extract_candidate_phrases("Assalomu alaykum, yaxshimisiz?")
    assert "assalomu alaykum" in extracted
    assert "assalomu" in extracted
    assert "yaxshimisiz" in extracted

    # 2. Contains entity name -> must be rejected
    phrases = filter_and_rank_phrases(
        candidate_counts=Counter({"assalomu alaykum": 10, "salom mansur": 10, "ali aka": 8}),
        contact_spread={"assalomu alaykum": {1, 2, 3, 4, 5}, "salom mansur": {1, 2, 3, 4, 5}, "ali aka": {1, 2, 3}},
        forbidden_entities=forbidden,
        min_count=5,
        min_contacts=3,
        eligible_contacts_count=5,
        min_eligible_contacts=5,
    )
    assert "assalomu alaykum" in phrases
    assert "salom mansur" not in phrases
    assert "ali aka" not in phrases

    # 3. Contains digits or URLs -> must be rejected
    phrases_with_bad = filter_and_rank_phrases(
        candidate_counts=Counter({"soat 10 da": 10, "https site": 10, "yaxshi rahmat": 10}),
        contact_spread={"soat 10 da": {1, 2, 3, 4, 5}, "https site": {1, 2, 3, 4, 5}, "yaxshi rahmat": {1, 2, 3, 4, 5}},
        forbidden_entities=set(),
        min_count=5,
        min_contacts=3,
        eligible_contacts_count=5,
        min_eligible_contacts=5,
    )
    assert "yaxshi rahmat" in phrases_with_bad
    assert "soat 10 da" not in phrases_with_bad
    assert "https site" not in phrases_with_bad


def test_phrases_disabled_when_eligible_contacts_below_threshold():
    # If fewer than 5 eligible contacts exist, global/register phrases are disabled
    phrases = filter_and_rank_phrases(
        candidate_counts=Counter({"assalomu alaykum": 20}),
        contact_spread={"assalomu alaykum": {1, 2, 3}},
        forbidden_entities=set(),
        min_count=5,
        min_contacts=3,
        eligible_contacts_count=3,  # only 3 eligible contacts! (< STYLE_PHRASE_MIN_ELIGIBLE_CONTACTS=5)
        min_eligible_contacts=5,
    )
    assert phrases == []


def test_build_style_profile_with_fallbacks():
    # If samples < STYLE_MIN_LANG_SAMPLES (60), falls back or returns None
    small_samples = [{"contact_user_id": 1, "text_redacted": "Salom", "char_len": 5, "token_len": 1, "lang": "uz-lat", "register": "sen"}] * 10

    prof = build_style_profile(
        scope="global",
        lang="uz-lat",
        samples=small_samples,
        forbidden_entities=set(),
        min_samples=60,
    )
    assert prof is None  # Not enough samples for a valid profile

    # With enough samples (>= 60)
    large_samples = [
        {"contact_user_id": (i % 6) + 1, "text_redacted": "Salom, yaxshimisiz?", "char_len": 19, "token_len": 2, "lang": "uz-lat", "register": "siz"}
        for i in range(70)
    ]
    prof_ok = build_style_profile(
        scope="global",
        lang="uz-lat",
        samples=large_samples,
        forbidden_entities=set(),
        min_samples=60,
    )
    assert prof_ok is not None
    assert prof_ok["sample_count"] == 70
    assert "avg_chars" in prof_ok["stats"]
