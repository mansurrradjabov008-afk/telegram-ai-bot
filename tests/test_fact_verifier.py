"""
Tests for Deterministic Fact Verifier and Confidence Derivation (Step 1.5, 1.6).
Verifies:
- Fact 'Python dasturchi' citing messages without 'Python' must be rejected.
- Fact with numbers not in source messages must be rejected.
- Grounded facts pass with derived confidence (not hardcoded 1.0).
- source_type confidence levels (owner_confirmed=1.0, owner_stated~0.95, contact_claim~0.85).
"""

import pytest
from database.models import MemMessage
from services.memory_fact_verifier import verify_fact_content


def test_fact_verifier_rejects_python_dasturchi_without_python():
    """Fact 'Python dasturchi' citing messages without 'Python' must be rejected."""
    msg = MemMessage(
        id=1,
        contact_user_id=101,
        tg_message_id=501,
        sender="contact",
        text_redacted="Salom, men dasturchi bo'lib ishlayman.",
        sent_at="2026-10-10 10:00:00",
    )
    # The word "Python" is not in the source message!
    is_valid, conf, reason = verify_fact_content(
        fact_text="Python dasturchi",
        source_messages=[msg],
        source_type="contact_claim",
    )
    assert is_valid is False
    assert "fact_verifier:missing_proper_noun:Python" in reason
    assert conf == 0.0


def test_fact_verifier_rejects_missing_number():
    """Fact containing a number not present in source messages must be rejected."""
    msg = MemMessage(
        id=2,
        contact_user_id=101,
        tg_message_id=502,
        sender="contact",
        text_redacted="Mening yangi ofisim Chilonzorda joylashgan.",
        sent_at="2026-10-10 10:05:00",
    )
    # Number 42 is not in source message
    is_valid, conf, reason = verify_fact_content(
        fact_text="Chilonzor 42 uy",
        source_messages=[msg],
        source_type="contact_claim",
    )
    assert is_valid is False
    assert "missing_number:42" in reason


def test_fact_verifier_accepts_grounded_fact_with_derived_confidence():
    """Grounded fact where all tokens appear in source message passes with derived confidence."""
    msg = MemMessage(
        id=3,
        contact_user_id=101,
        tg_message_id=503,
        sender="contact",
        text_redacted="Men Chilonzorda IT kompaniyasida Frontend dasturchi bo'lib ishlayman.",
        sent_at="2026-10-10 10:10:00",
    )
    is_valid, conf, status = verify_fact_content(
        fact_text="Chilonzorda Frontend dasturchi",
        source_messages=[msg],
        source_type="contact_claim",
    )
    assert is_valid is True
    assert status == "verified"
    # Derived confidence must not be hardcoded 1.0 for contact_claim
    assert 0.65 <= conf <= 0.85
    assert conf != 1.0


def test_fact_verifier_confidence_by_source_type():
    """Confidence reflects source_type grounding accurately."""
    msg = MemMessage(
        id=4,
        contact_user_id=101,
        tg_message_id=504,
        sender="owner_manual",
        text_redacted="Alisher Toshkent shahrida yashaydi.",
        sent_at="2026-10-10 10:15:00",
    )
    # owner_stated
    ok_owner, conf_owner, _ = verify_fact_content("Toshkent shahrida yashaydi", [msg], source_type="owner_stated")
    assert ok_owner is True
    assert 0.85 <= conf_owner <= 0.95

    # owner_confirmed
    ok_conf, conf_conf, _ = verify_fact_content("Toshkent shahrida yashaydi", [msg], source_type="owner_confirmed")
    assert ok_conf is True
    assert conf_conf == 1.0


def test_fact_verifier_uzbek_kinship_and_meta_words():
    """Verifies that Uzbek kinship (uka, aka) and meta references (Mansurni, murojaat) verify accurately."""
    msg = MemMessage(
        id=5,
        contact_user_id=8036556250,
        tg_message_id=505,
        sender="contact",
        text_redacted="Assalomu alaykum yaxshimisiz ukam, bugun vaqting bormi",
        sent_at="2026-10-14 10:00:00",
    )
    ok, conf, status = verify_fact_content(
        fact_text="Kontakt Mansurni uka deb ataydi",
        source_messages=[msg],
        source_type="contact_claim",
    )
    assert ok is True
    assert status == "verified"
    assert 0.65 <= conf <= 0.85


def test_fact_verifier_uzbek_phonetic_variation_and_suffixes():
    """Verifies that Uzbek phonetic variations (Ingliz vs ingilizchasiga) verify properly."""
    msg = MemMessage(
        id=6,
        contact_user_id=8036556250,
        tg_message_id=506,
        sender="contact",
        text_redacted="Menga kitob kerak ingilizchasiga nima bular edi",
        sent_at="2026-10-14 10:05:00",
    )
    ok, conf, status = verify_fact_content(
        fact_text="Ingliz tili so'zlariga qiziqadi",
        source_messages=[msg],
        source_type="contact_claim",
    )
    assert ok is True
    assert status == "verified"
    assert 0.65 <= conf <= 0.85

