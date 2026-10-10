"""
Tests for Writing Style Hygiene Filtering and Language/Register Detection (D2, D3).
"""

from datetime import datetime, timedelta
import pytest
from services.style_sampler import (
    is_hygienic_sample,
    detect_language_script,
    detect_register,
    normalize_sample_text,
    HygieneCheckResult,
)


def test_hygiene_drops_forwarded():
    res = is_hygienic_sample(
        text="Salom, qalaysiz?",
        is_forward=True,
    )
    assert not res.passed
    assert "forwarded" in res.reason


def test_hygiene_drops_via_bot():
    res = is_hygienic_sample(
        text="Salom, qalaysiz?",
        via_bot=True,
    )
    assert not res.passed
    assert "via_bot" in res.reason


def test_hygiene_drops_service():
    res = is_hygienic_sample(
        text="Pinned message",
        is_service=True,
    )
    assert not res.passed
    assert "service" in res.reason


def test_hygiene_drops_media_only():
    res = is_hygienic_sample(
        text="",
        is_media_only=True,
    )
    assert not res.passed
    assert "media_only" in res.reason

    # Also if text is empty or only media marker
    res2 = is_hygienic_sample(
        text="[rasm]",
        is_media_only=False,
    )
    assert not res2.passed
    assert "media_marker" in res2.reason or "media_only" in res2.reason


def test_hygiene_drops_contains_redacted():
    res = is_hygienic_sample(
        text="Mening kartam [REDACTED] raqamida.",
    )
    assert not res.passed
    assert "redacted" in res.reason


def test_hygiene_drops_exceeds_max_chars():
    long_text = "a" * 401
    res = is_hygienic_sample(
        text=long_text,
        max_chars=400,
    )
    assert not res.passed
    assert "max_chars" in res.reason


def test_hygiene_drops_url_heavy():
    # More than 1 URL or >30% URL content
    res = is_hygienic_sample(
        text="Ko'ring https://example.com/one va https://example.com/two",
    )
    assert not res.passed
    assert "url" in res.reason


def test_hygiene_drops_starts_with_slash():
    res = is_hygienic_sample(
        text="/start or /help buyrug'i",
    )
    assert not res.passed
    assert "command" in res.reason


def test_hygiene_drops_exact_duplicates():
    seen = {"salom qalaysiz"}
    res = is_hygienic_sample(
        text="Salom, qalaysiz!",
        seen_normalized=seen,
    )
    assert not res.passed
    assert "duplicate" in res.reason


def test_hygiene_drops_outside_window_days():
    old_date = datetime.now() - timedelta(days=181)
    res = is_hygienic_sample(
        text="Eski xabar",
        sent_at=old_date,
        window_days=180,
    )
    assert not res.passed
    assert "window" in res.reason


def test_hygiene_drops_bot_sent_collision():
    bot_sent = {"rahmat yaxshi o'zingizchi"}
    res = is_hygienic_sample(
        text="Rahmat, yaxshi, o'zingizchi?",
        bot_sent_normalized=bot_sent,
    )
    assert not res.passed
    assert "bot_collision" in res.reason


def test_hygiene_accepts_clean_owner_message():
    res = is_hygienic_sample(
        text="Ertaga soat 10 da ko'rishamiz, bo'ladimi?",
        sent_at=datetime.now() - timedelta(days=5),
        seen_normalized=set(),
        bot_sent_normalized=set(),
    )
    assert res.passed
    assert res.reason == "ok"


def test_language_detection():
    assert detect_language_script("Assalomu alaykum, yaxshimisiz?") == "uz-lat"
    assert detect_language_script("Ассалому алайкум, яхшимисиз?") == "uz-cyr"
    assert detect_language_script("Привет, как ваши дела сегодня?") == "ru"
    assert detect_language_script("Hello, how are you doing today?") == "en"
    assert detect_language_script("123456 !!! ???") == "other"


def test_register_detection():
    assert detect_register("Yaxshimisiz, ishlaringiz qalay?") == "siz"
    assert detect_register("Qalaysan, nima qilyapsan?") == "sen"
    assert detect_register("Salom!") is None
