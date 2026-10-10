"""
Unit and integration tests for Style Pipeline, Honesty Guard, and Timeout (D6, D7, D8, Phase 5).
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import pytest
import pytest_asyncio

from database.connection import init_db
from database.crud import DatabaseManager
from services.style_service import StyleService
from config import settings


@pytest_asyncio.fixture
async def temp_pipeline_db(tmp_path: Path):
    temp_path = tmp_path / "test_style_pipeline.db"
    await init_db(temp_path)
    db_mgr = DatabaseManager(temp_path)
    await db_mgr.init_style_schema()
    return db_mgr, temp_path


def test_honesty_check_lexicon():
    service = StyleService(db_manager=None)

    # Uzbek Latin
    assert service.check_honesty_question("Sen botmisan yoki o'zing yozayapsanmi?") is True
    assert service.check_honesty_question("Haqiqiy Mansurmisan?") is True
    assert service.check_honesty_question("Avto-javobmi bu?") is True

    # Uzbek Cyrillic
    assert service.check_honesty_question("Сен ботмисан?") is True
    assert service.check_honesty_question("Ўзинг ёзяпсанми?") is True

    # Russian
    assert service.check_honesty_question("Ты бот или человек?") is True
    assert service.check_honesty_question("Это автоответчик?") is True

    # English
    assert service.check_honesty_question("Are you a bot?") is True
    assert service.check_honesty_question("Is this an AI responding?") is True

    # Regular messages -> False
    assert service.check_honesty_question("Salom, qandaysiz?") is False
    assert service.check_honesty_question("Ertaga uchrashamizmi?") is False
    assert service.check_honesty_question("Kitobni olib keldingizmi?") is False


@pytest.mark.asyncio
async def test_style_pipeline_disabled_by_default(temp_pipeline_db):
    db_mgr, _ = temp_pipeline_db
    service = StyleService(db_manager=db_mgr)

    # By default STYLE_ENABLED is False and no contact override
    enabled = await service.is_style_enabled_for_contact(1001)
    assert enabled is False

    ctx = await service.get_style_context(contact_user_id=1001, incoming_text="Salom")
    assert ctx == ""


@pytest.mark.asyncio
async def test_style_pipeline_per_contact_enable(temp_pipeline_db):
    db_mgr, _ = temp_pipeline_db
    service = StyleService(db_manager=db_mgr)

    # Enable for contact 1001
    await service.set_contact_enabled(1001, True)
    assert await service.is_style_enabled_for_contact(1001) is True
    assert await service.is_style_enabled_for_contact(1002) is False

    # Seed some samples
    for i in range(10):
        await db_mgr.insert_style_sample(
            contact_user_id=1001,
            tg_message_id=i,
            text_redacted=f"Salom {i}",
            char_len=7,
            token_len=2,
            lang="uz-lat",
            register="siz",
            sent_at=datetime.now().isoformat(),
            source="live",
        )

    ctx = await service.get_style_context(contact_user_id=1001, incoming_text="Salom qalaysiz")
    assert "XO'JAYINNING YOZISH USLUBI" in ctx


@pytest.mark.asyncio
async def test_style_pipeline_timeout_fails_open(temp_pipeline_db):
    db_mgr, _ = temp_pipeline_db
    service = StyleService(db_manager=db_mgr)
    await service.set_contact_enabled(1001, True)

    # Force a delay exceeding timeout
    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(0.5)
        return "slow"

    service._fetch_internal = slow_fetch

    # With timeout 0.05s, must fail open and return "" without raising
    ctx = await service.get_style_context(contact_user_id=1001, incoming_text="Salom", timeout=0.05)
    assert ctx == ""
