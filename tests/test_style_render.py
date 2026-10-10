"""
Unit tests for Writing Style Guide Renderer (D5, D6, D7, Phase 4).
"""

from datetime import datetime, timedelta
from pathlib import Path
import pytest
import pytest_asyncio

from database.connection import init_db
from database.crud import DatabaseManager
from services.style_renderer import (
    render_style_guide,
    filter_few_shot_examples,
)


def test_empty_profile_produces_clean_fallback_guide():
    guide = render_style_guide(
        profile=None,
        target_lang="uz-lat",
    )
    assert "USLUB KO'RSATMASI" in guide
    assert "Qat'iy chegara" in guide
    # Token count / length should be very concise
    assert len(guide.split()) < 200


def test_render_style_guide_with_full_profile_and_stats():
    profile = {
        "scope": "global",
        "lang": "uz-lat",
        "sample_count": 100,
        "stats": {
            "avg_chars": 40.0,
            "p50_chars": 35.0,
            "p90_chars": 80.0,
            "min_chars": 5,
            "max_chars": 120,
            "avg_words": 6.0,
            "p50_words": 5.0,
            "p90_words": 12.0,
            "min_words": 1,
            "max_words": 20,
            "single_excl_rate": 0.05,
            "multi_excl_rate": 0.0,
            "ellipsis_rate": 0.1,
            "question_rate": 0.2,
            "avg_emojis": 0.8,
            "p90_emojis": 1.0,
            "top_emojis": [("👍", 40), ("🤝", 20)],
            "emoji_at_end_rate": 0.9,
            "all_lowercase_rate": 0.85,
            "standard_sentence_case_rate": 0.10,
            "siz_ratio": 0.7,
            "sen_ratio": 0.3,
        },
        "phrases": ["assalomu alaykum", "rahmat katta", "xop boladi"],
    }

    guide = render_style_guide(
        profile=profile,
        examples=["Ertaga ertalab gaplashamiz.", "Rahmat, yaxshi."],
        owner_note="Iloji boricha kamroq so'z bilan javob bering.",
        target_lang="uz-lat",
    )

    # 1. Length checks
    # hard cap = max(300, 3 * p90_chars) = max(300, 240) = 300
    assert "300" in guide

    # 2. Lowercase rule
    assert "kichik harflar" in guide.lower()

    # 3. Phrases
    assert "assalomu alaykum" in guide

    # 4. Few-shot notice
    assert "mavzu yoki ma'lumotlarni ASLO ko'chirmang" in guide
    assert "Ertaga ertalab gaplashamiz." in guide

    # 5. Owner note renders below safety boundary (D7)
    assert "XAVFSIZLIK CHEGARASI" in guide
    safety_idx = guide.index("XAVFSIZLIK CHEGARASI")
    note_idx = guide.index("Iloji boricha kamroq so'z bilan javob bering.")
    assert note_idx > safety_idx

    # 6. Token limit check (< STYLE_GUIDE_MAX_TOKENS ~250 words/tokens)
    assert len(guide.split()) <= 250


def test_few_shot_sensitive_filter_blocks_money_promises_meetings():
    # Synthetic candidate messages:
    candidates = [
        "Ertaga soat 10 da ko'rishamiz.",  # Contains meeting/time -> sensitive -> DROP
        "100 dollar beraman.",            # Money/digits -> sensitive -> DROP
        "Va'da beraman, ertaga bitadi.",   # Promise -> sensitive -> DROP
        "Rahmat, yaxshi, o'zingizdachi?",  # Clean -> ACCEPT
        "Xop, kelishdik, aloqadamiz.",     # Clean -> ACCEPT
    ]

    filtered = filter_few_shot_examples(candidates, max_examples=3, max_chars=120)
    assert len(filtered) == 2
    assert "Rahmat, yaxshi, o'zingizdachi?" in filtered
    assert "Xop, kelishdik, aloqadamiz." in filtered
    assert "100 dollar beraman." not in filtered
    assert "Va'da beraman, ertaga bitadi." not in filtered
