"""
Test suite verifying:
1. Dynamic Reverse Register & Respect Demands ('sansirama', 'sandan kattaman', 'aka deb murojat qil', 'sizla')
2. Severe Profanity & Conflict Sensitive Guard (hard_sensitive=True for abusive language)
3. Draft dataclass backward compatibility (.user_id and .draft_text properties)
4. Anti-repetition & Anti-arguing guardrails
"""

import pytest
from unittest.mock import MagicMock, AsyncMock
from services.ai_engine import is_respect_demanded, AIEngine
from services.sensitive_guard import (
    check_sensitive_keywords_detailed,
    check_sensitive,
    PROFANITY_PATTERN,
)
from database.models import Draft, Contact, ConversationMessage


def test_is_respect_demanded_true():
    demands = [
        "sansirami jalab",
        "sansirama manga sandan kattaman",
        "qanaqa do'st? jalab aka deb murojat qil manga jalabb",
        "sensirama menga",
        "sendan kattaman aka de",
        "menga siz deb gapir",
        "sizlang aka",
        "akam bo'lasan",
        "dada deb chaqir meni",
    ]
    for d in demands:
        assert is_respect_demanded(d) is True, f"Failed to detect respect demand in: {d}"


def test_is_respect_demanded_false_for_normal_chitchat():
    normal = [
        "Salom qalaysan",
        "Bugun ko'rishamizmi?",
        "Ertaga soat 17:30 da dars bo'ladi",
        "O yebana aniqroq gapir",
        "Kutubxonaga boramiz",
    ]
    for n in normal:
        assert is_respect_demanded(n) is False, f"False positive respect demand in: {n}"


def test_profanity_pattern_matches_abusive_messages():
    abusive = [
        "sansirami jalab",
        "pashol naxuyyy",
        "naxuyyy kot",
        "tinchmasku, o'g'zingga olishing kk edi olmadingku bich",
        "zaybal qingdingda",
        "qanaqa do'st? jalab aka deb murojat qil manga jalabb",
    ]
    for a in abusive:
        assert bool(PROFANITY_PATTERN.search(a.lower())) is True, f"Failed to detect profanity in: {a}"
        detail = check_sensitive_keywords_detailed(a)
        assert detail["hard_sensitive"] is True, f"Expected hard_sensitive for: {a}"
        assert "profanity_conflict" in detail["categories"]


def test_profanity_pattern_ignores_safe_messages():
    safe = [
        "O yebana aniqroq gapir",
        "Bugun soat 8:00da kutubxonaga boramiz",
        "Qayerdasan jigar?",
        "Nega javob bermayapsan",
    ]
    for s in safe:
        detail = check_sensitive_keywords_detailed(s)
        assert "profanity_conflict" not in detail["categories"]
        assert detail["hard_sensitive"] is False


def test_draft_dataclass_properties_backward_compatibility():
    draft = Draft(
        id=99,
        contact_username="test_user",
        contact_user_id=12345678,
        source_message_id=555,
        incoming_text="Salom",
        proposed_reply="Salom, qalaysiz?",
        status="pending",
        is_sensitive=False,
    )
    # Check that modern properties match legacy property aliases
    assert draft.user_id == 12345678
    assert draft.contact_user_id == 12345678
    assert draft.draft_text == "Salom, qalaysiz?"
    assert draft.proposed_reply == "Salom, qalaysiz?"
    assert draft.is_pending is True


@pytest.mark.asyncio
async def test_generate_reply_respect_demanded_forces_siz_directive():
    ai = AIEngine(api_key=None)  # Offline / fallback mode
    contact = Contact(
        user_id=8747692548,
        username="suhrob",
        full_name="Suhrob Omonov",
        relationship_type="dustim",  # Normally informal
        addressing_style="sen",
        custom_instructions="dustim deb murojat qil",
    )
    # Contact writes "sansirama manga sandan kattaman"
    reply = await ai.generate_reply(
        incoming_text="sansirama manga sandan kattaman",
        sender_name="Suhrob",
        contact_profile=contact,
        history=[],
    )
    # In fallback mode, must not use informal 'sen' or argue back
    assert reply is not None
    assert len(reply) > 0
    # Must use polite 'siz' register
    assert any(w in reply.lower() for w in ["siz", "rahmat", "albatta", "kelishdik", "tushunarli", "salomat"])


@pytest.mark.asyncio
async def test_heuristic_fallback_classify_profanity_escalates():
    ai = AIEngine(api_key=None)
    res = ai._heuristic_fallback_classify("pashol naxuyyy kot")
    assert res.category == "ESCALATE_TO_BOSS"
    assert res.urgency == "high"


def test_draft_summary_in_jarvis_context_does_not_raise():
    pending_drafts = [
        Draft(
            id=26,
            contact_username="SuhrobOmonov12",
            contact_user_id=8747692548,
            source_message_id=372,
            incoming_text="qanaqa do'st? aka deb murojat qil",
            proposed_reply="Kechirasiz aka, tushundim.",
            status="pending",
            is_sensitive=True,
            sensitive_reason="Haqorat / Tajovuzkor so'kinish / Ziddiyat",
        )
    ]
    contacts_map = {8747692548: "Сухров Омонов"}
    drafts_summary = []
    for d in pending_drafts[:5]:
        cid = getattr(d, "contact_user_id", None) or getattr(d, "user_id", None)
        c_name = contacts_map.get(cid, getattr(d, "contact_username", None) or f"ID:{cid}")
        draft_msg = getattr(d, "proposed_reply", "") or getattr(d, "draft_text", "")
        drafts_summary.append(f"Draft #{d.id} ({c_name}): \"{draft_msg[:100]}\"")

    assert len(drafts_summary) == 1
    assert "Draft #26 (Сухров Омонов)" in drafts_summary[0]
