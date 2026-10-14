"""
Tests for Smart Fact Consolidation, Deduplication and Merging.
"""

import pytest
from database.models import MemFact
from services.memory_extractor import consolidate_fact_with_existing


def test_exact_duplicate_rejected():
    ef = MemFact(
        id=1,
        contact_user_id=100,
        category="interest",
        text="Ingliz tili so'zlariga qiziqadi",
        source_type="contact_claim",
    )
    is_dup, final_text, sup_id = consolidate_fact_with_existing(
        fact_text="Ingliz tili so'zlariga qiziqadi",
        category="interest",
        existing_facts=[ef],
    )
    assert is_dup is True
    assert sup_id is None


def test_relationship_addressing_terms_merged():
    ef = MemFact(
        id=2,
        contact_user_id=100,
        category="relationship_note",
        text="Kontakt egasiga uka deb murojaat qiladi",
        source_type="contact_claim",
    )
    is_dup, final_text, sup_id = consolidate_fact_with_existing(
        fact_text="Kontakt egasiga bro deb murojaat qiladi",
        category="relationship_note",
        existing_facts=[ef],
    )
    assert is_dup is False
    assert sup_id == 2
    assert "uka" in final_text
    assert "bro" in final_text


def test_semantic_preference_overlap_superseded():
    ef = MemFact(
        id=3,
        contact_user_id=100,
        category="preference",
        text="Kutubxona o'rniga aylanishga chiqishni ma'qul ko'radi",
        source_type="contact_claim",
    )
    # Richer variation on same preference
    is_dup, final_text, sup_id = consolidate_fact_with_existing(
        fact_text="Kutubxonaga borish o'rniga shunchaki aylanishga chiqishni ma'qul ko'radi",
        category="preference",
        existing_facts=[ef],
    )
    assert is_dup is False
    assert sup_id == 3
    assert "shunchaki" in final_text


def test_shorter_subset_skipped_if_longer_exists():
    ef = MemFact(
        id=4,
        contact_user_id=100,
        category="interest",
        text="Ingliz tili so'zlarini o'rganishga qiziqadi va tarjima so'raydi",
        source_type="contact_claim",
    )
    is_dup, final_text, sup_id = consolidate_fact_with_existing(
        fact_text="Ingliz tili so'zlariga qiziqadi",
        category="interest",
        existing_facts=[ef],
    )
    assert is_dup is True
    assert sup_id is None


def test_temporal_category_supersedes():
    ef = MemFact(
        id=5,
        contact_user_id=100,
        category="ongoing_topic",
        text="Hozircha kitob o'qigani yo'q",
        source_type="contact_claim",
    )
    is_dup, final_text, sup_id = consolidate_fact_with_existing(
        fact_text="Yangi IT kitobini o'qishni boshladi",
        category="ongoing_topic",
        existing_facts=[ef],
    )
    assert is_dup is False
    assert sup_id == 5
    assert final_text == "Yangi IT kitobini o'qishni boshladi"
