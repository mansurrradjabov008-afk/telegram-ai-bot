import pytest
from services.ai_engine import AIEngine, extract_clean_human_name
from services.digest_cleaner import transliterate_uzbek_cyrillic_to_latin
from database.models import ConversationMessage

@pytest.mark.asyncio
async def test_cyrillic_name_cleanup():
    raw_name = "Сухроб Омонов"
    latin_name = transliterate_uzbek_cyrillic_to_latin(raw_name)
    assert latin_name == "Suxrob Omonov"
    clean_first = extract_clean_human_name(latin_name)
    assert clean_first == "Suxrob"

@pytest.mark.asyncio
async def test_clarification_query_classification_is_safe_chitchat():
    engine = AIEngine(api_key="")
    
    # "Qaysi loyiha?" must NOT escalate to Boss!
    res = await engine.classify_incoming_message(
        incoming_text="Qaysi loyiha?",
        sender_name="Suxrob",
    )
    assert res.category == "SAFE_CHITCHAT"
    assert "aniqlik" in res.reason.lower() or "do'stona" in res.reason.lower()

    # Slang "Qandayeee" must also be SAFE_CHITCHAT
    res_slang = await engine.classify_incoming_message(
        incoming_text="Qandayeee",
        sender_name="Suxrob",
    )
    assert res_slang.category == "SAFE_CHITCHAT"

@pytest.mark.asyncio
async def test_clarification_smart_fallback_reply():
    engine = AIEngine(api_key="")
    
    # Testing SEN mode
    reply_sen = engine._dynamic_smart_fallback("Qaysi loyiha?", addressing="sen")
    assert "kechagi" in reply_sen.lower() or "mavzu" in reply_sen.lower()
    assert "siz" not in reply_sen.lower()

    # Testing SIZ mode
    reply_siz = engine._dynamic_smart_fallback("Qaysi loyiha?", addressing="siz")
    assert "kecha" in reply_siz.lower() or "mavzu" in reply_siz.lower()
