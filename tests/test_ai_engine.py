"""
Unit tests for AI Engine classification logic, guardrails, and rate limiter.
"""

import pytest
from services.ai_engine import AIEngine, ClassificationResult
from services.rate_limiter import HumanRateLimiter
from database.models import Contact


@pytest.mark.asyncio
async def test_ai_engine_heuristic_classification():
    engine = AIEngine(api_key="")

    # 1. Routine chit-chat test
    res1 = await engine.classify_incoming_message(
        incoming_text="Salom, qalaysan? Ishlar yaxshimi?",
        sender_name="Ali",
    )
    assert res1.category == "SAFE_CHITCHAT"
    assert res1.confidence >= 0.8

    # 2. Financial escalation test
    res2 = await engine.classify_incoming_message(
        incoming_text="Assalomu alaykum, kechagi 500$ to'lov nima bo'ldi?",
        sender_name="Vali",
    )
    assert res2.category == "ESCALATE_TO_BOSS"
    assert res2.urgency == "high"

    # 3. Meeting scheduling escalation test
    res3 = await engine.classify_incoming_message(
        incoming_text="Ertaga soat nechida uchrashuv qilamiz?",
        sender_name="Sherzod",
    )
    assert res3.category == "ESCALATE_TO_BOSS"


def test_rate_limiter_typing_simulation():
    limiter = HumanRateLimiter(min_delay=2, max_delay=4)

    short_text = "Salom!"
    duration_short = limiter.calculate_typing_duration(short_text)
    assert 2.0 <= duration_short <= 12.0

    long_text = "A" * 300
    duration_long = limiter.calculate_typing_duration(long_text)
    assert duration_long <= 12.0
    assert duration_long > duration_short
