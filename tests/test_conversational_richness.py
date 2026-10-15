import pytest
from unittest.mock import AsyncMock, MagicMock
from services.ai_engine import AIEngine
from database.models import Contact, ConversationMessage


@pytest.mark.asyncio
async def test_anti_repetition_directive_injected_when_history_present():
    """Verify that generate_reply captures previous assistant messages and injects anti-repetition rules."""
    ai = AIEngine()
    ai._get_candidate_models = MagicMock(return_value=["test-model"])
    ai._client = MagicMock()

    captured_prompts = []
    captured_kwargs = []

    async def mock_raw(model, prompt, **kwargs):
        captured_prompts.append(prompt)
        captured_kwargs.append(kwargs)
        return "Eee nima gap jigar, yangi loyihani boshlayapmiz!"

    ai._call_gemini_raw = AsyncMock(side_effect=mock_raw)

    contact = Contact(
        user_id=555123,
        username="alisher_dev",
        full_name="Alisher",
        relationship_type="bro",
        addressing_style="sen",
        mode="auto",
    )

    history = [
        ConversationMessage(
            id=1,
            chat_id=555123,
            sender_name="Alisher",
            role="contact",
            content="Salom Mansur, qandaysan?",
            timestamp="2026-10-15T12:00:00",
        ),
        ConversationMessage(
            id=2,
            chat_id=555123,
            sender_name="Mansur",
            role="assistant",
            content="Salom bro, yaxshi shukr, o'zingdachi?",
            timestamp="2026-10-15T12:01:00",
        ),
        ConversationMessage(
            id=3,
            chat_id=555123,
            sender_name="Alisher",
            role="contact",
            content="Zo'r, nimalar qilyapsan?",
            timestamp="2026-10-15T12:02:00",
        ),
    ]

    reply = await ai.generate_reply(
        incoming_text="Zo'r, nimalar qilyapsan?",
        sender_name="Alisher",
        contact_profile=contact,
        history=history,
    )

    assert "Eee nima gap jigar" in reply
    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]

    # Verify Anti-Repetition block is present and contains the previous phrase
    assert "BIR GAPNI CHAYNASH VA TAKRORLASH 100% TAQIQLANADI" in prompt
    assert 'Salom bro, yaxshi shukr, o\'zingdachi?' in prompt
    assert "YANGI XABARDA QAYTA SALOM BERMANG" in prompt

    # Verify Temperature and Top-P are configured for rich natural vocabulary
    kwargs = captured_kwargs[0]
    assert kwargs.get("temperature") == 0.75
    assert kwargs.get("top_p") == 0.95


@pytest.mark.asyncio
async def test_vast_lexicon_thesaurus_in_prompt():
    """Verify that rich Uzbek expressions, emotional reactions, and Turing test defense are in prompt."""
    ai = AIEngine()
    ai._get_candidate_models = MagicMock(return_value=["test-model"])
    ai._client = MagicMock()

    captured_prompts = []

    async def mock_raw(model, prompt, **kwargs):
        captured_prompts.append(prompt)
        return "Albatta, qoyil lekin!"

    ai._call_gemini_raw = AsyncMock(side_effect=mock_raw)

    contact = Contact(
        user_id=777,
        username="sanjar",
        full_name="Sanjar",
        addressing_style="siz",
        mode="auto",
    )

    await ai.generate_reply(
        incoming_text="Assalomu alaykum Mansur aka",
        sender_name="Sanjar",
        contact_profile=contact,
        history=[],
    )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]

    # Check for vast lexicon headers
    assert "CHEKSIZ SO'Z BOYLIGI VA TIRIK INSONIY REAKSIYA" in prompt
    assert "TURING PROOF" in prompt
    assert "Voy-bo'" in prompt
    assert "Qoyil!" in prompt
    assert "SUHBAT RITMI" in prompt


@pytest.mark.asyncio
async def test_dynamic_smart_fallback_variety_and_intent():
    """Verify _dynamic_smart_fallback handles identity queries, activity queries, and diverse options."""
    engine = AIEngine(api_key="")

    # 1. Identity query in SEN mode
    reply_bot_sen = engine._dynamic_smart_fallback("Botmisan?", addressing="sen")
    assert any(w in reply_bot_sen.lower() for w in ["o'zimman", "yozyapman", "qo'ysang-chi"])
    assert "siz" not in reply_bot_sen.lower()

    # 2. Identity query in SIZ mode
    reply_bot_siz = engine._dynamic_smart_fallback("Botmisiz?", addressing="siz")
    assert any(w in reply_bot_siz.lower() for w in ["o'zim", "yozmoqdaman"])

    # 3. Status/activity in SEN mode
    reply_act_sen = engine._dynamic_smart_fallback("Nima qilyapsan qayerdasan?", addressing="sen")
    assert any(w in reply_act_sen.lower() for w in ["yo'ldaman", "ishlar", "bo'shamay", "harakatdamiz", "loyiha"])
    assert "siz" not in reply_act_sen.lower()

    # 4. Status/activity in SIZ mode
    reply_act_siz = engine._dynamic_smart_fallback("Qayerdasiz?", addressing="siz")
    assert any(w in reply_act_siz.lower() for w in ["yo'ldaman", "ishlar", "loyihalar", "bandroq"])

    # 5. Clarification query preserves test contract
    reply_clar_sen = engine._dynamic_smart_fallback("Qaysi loyiha?", addressing="sen")
    assert "kechagi" in reply_clar_sen.lower() or "mavzu" in reply_clar_sen.lower()
    assert "siz" not in reply_clar_sen.lower()

    reply_clar_siz = engine._dynamic_smart_fallback("Qaysi loyiha?", addressing="siz")
    assert "kecha" in reply_clar_siz.lower() or "mavzu" in reply_clar_siz.lower()
