"""
Unit tests for Smart Digest V2 - Phase 3: Classification.
Tests schema validation, retry-once logic, anti-hallucination post_id checks,
precedence rules, structured extraction for Topics D and F, quota limits,
and database persistence.
"""

import json
import pytest
import pytest_asyncio
import aiosqlite
from unittest.mock import AsyncMock, MagicMock, patch

from config import settings
from database.connection import init_db, get_db
from database.crud import DatabaseManager
from database.models import DigestPost
from services.ai_engine import AIEngine
from services.digest_classifier import (
    DigestClassifier,
    TopicDExtracted,
    TopicFExtracted,
    ClassifiedPostItem,
    clean_json_text,
)
from services.digest_service import DigestService


@pytest_asyncio.fixture
async def temp_db_crud(tmp_path):
    """Initializes isolated database instance for Phase 3 testing."""
    test_db = tmp_path / "test_phase3.db"
    await init_db(test_db)
    crud = DatabaseManager(test_db)
    return crud, test_db


@pytest.mark.asyncio
async def test_schema_migration_v5_and_column_presence(temp_db_crud):
    """Verifies migration v5 adds extracted_json to digest_posts and updates schema_versions."""
    crud, test_db = temp_db_crud

    max_v = await crud.get_schema_version()
    assert max_v >= 5

    async with get_db(test_db) as db:
        async with db.execute("PRAGMA table_info(digest_posts)") as cur:
            cols = [row["name"] for row in await cur.fetchall()]
        assert "extracted_json" in cols


@pytest.mark.asyncio
async def test_digest_posts_classification_crud(temp_db_crud):
    """Tests updating classification topic, importance, and extracted_json in DB."""
    crud, _ = temp_db_crud

    post = DigestPost(
        chat_id=-100111,
        message_id=501,
        chat_title="Vakansiyalar",
        text="Python Senior Developer kerak. Maosh: $4000 oyiga.",
        status="new",
    )
    post_id = await crud.save_digest_post(post)
    assert post_id is not None

    ext_data = {
        "profession": "Python Senior Developer",
        "amount": "$4000",
        "currency": "USD",
        "period": "oyiga",
        "location": "Toshkent",
        "employer": "Tech Corp",
    }
    ext_json = json.dumps(ext_data)

    success = await crud.update_digest_post_classification(
        post_id=post_id,
        topic="D",
        importance=4,
        status="classified",
        extracted_json=ext_json,
    )
    assert success is True

    posts = await crud.get_digest_posts(status="classified")
    assert len(posts) == 1
    assert posts[0].topic == "D"
    assert posts[0].importance == 4
    assert posts[0].extracted_json == ext_json

    # Test summary breakdown
    summary = await crud.get_classified_posts_summary()
    assert summary.get("D") == 1


def test_clean_json_text_markdown_stripping():
    """Verifies markdown code block wrappers are stripped properly."""
    raw = "```json\n{\"items\": []}\n```"
    assert clean_json_text(raw) == "{\"items\": []}"

    raw2 = "  {\"items\": []}  "
    assert clean_json_text(raw2) == "{\"items\": []}"


def test_pydantic_extracted_schemas():
    """Tests TopicDExtracted and TopicFExtracted schemas with defaults."""
    d = TopicDExtracted(profession="ML Engineer", amount="$3,500", currency="USD")
    dump_d = d.model_dump()
    assert dump_d["profession"] == "ML Engineer"
    assert dump_d["amount"] == "$3,500"
    assert dump_d["period"] == "ko'rsatilmagan"

    f = TopicFExtracted(title="Erasmus+ Grant", deadline="15-noyabr")
    dump_f = f.model_dump()
    assert dump_f["title"] == "Erasmus+ Grant"
    assert dump_f["deadline"] == "15-noyabr"
    assert dump_f["how_to_apply"] == "ko'rsatilmagan"


def test_anti_hallucination_post_id_validation():
    """Anti-Hallucination verification: referenced post_id must exist in input batch."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    expected_ids = {101, 102}
    raw_response = json.dumps({
        "items": [
            {"post_id": 101, "topic": "E", "importance": 5, "is_advertisement": False},
            {"post_id": 999, "topic": "A", "importance": 4, "is_advertisement": False},  # Hallucinated post_id!
        ]
    })

    validated = classifier.parse_and_validate_response(raw_response, expected_ids)
    assert validated is not None
    assert len(validated) == 1
    assert validated[0].post_id == 101


@pytest.mark.asyncio
async def test_retry_once_on_invalid_output():
    """Tests retry-once mechanism: first attempt invalid, second succeeds."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    batch = [
        DigestPost(id=201, chat_id=-100, message_id=1, text="AI GPT-5 taqdimoti"),
    ]

    valid_json = json.dumps({
        "items": [
            {"post_id": 201, "topic": "E", "importance": 5, "is_advertisement": False}
        ]
    })

    # First call returns malformed JSON, second returns valid JSON
    ai_mock.generate_json_response = AsyncMock(side_effect=["Not JSON {invalid", valid_json])

    result = await classifier.classify_batch_with_retry(batch)
    assert result is not None
    assert len(result) == 1
    assert result[0].post_id == 201
    assert ai_mock.generate_json_response.call_count == 2


@pytest.mark.asyncio
async def test_retry_both_fail_no_hallucinated_fallback():
    """Verifies that if both attempts fail, batch is skipped without hallucinating fallback."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    batch = [
        DigestPost(id=301, chat_id=-100, message_id=2, text="Diplomatik muzokaralar"),
    ]

    ai_mock.generate_json_response = AsyncMock(side_effect=["Invalid 1", "Invalid 2"])

    result = await classifier.classify_batch_with_retry(batch)
    assert result is None
    assert ai_mock.generate_json_response.call_count == 2

    # Run full classify_posts
    ai_mock.generate_json_response = AsyncMock(side_effect=["Invalid 1", "Invalid 2"])
    res = await classifier.classify_posts(batch)
    assert res["failed_batches_count"] == 1
    assert res["total_classified"] == 0
    assert len(res["discarded_posts"]) == 1
    assert res["discarded_posts"][0].discard_reason == "ai_classification_failed"


@pytest.mark.asyncio
async def test_drop_other_importance_1_and_ads():
    """Verifies that 'other', importance <= 1, and advertisements are discarded."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    posts = [
        DigestPost(id=401, chat_id=-100, message_id=10, text="Ob-havo ma'lumoti"),
        DigestPost(id=402, chat_id=-100, message_id=11, text="Arzimas siyosiy mish-mish"),
        DigestPost(id=403, chat_id=-100, message_id=12, text="Krossovka sotiladi, aksiya!"),
        DigestPost(id=404, chat_id=-100, message_id=13, text="O'zbekiston BMTda yangi taklif kiritdi"),
    ]

    ai_resp = json.dumps({
        "items": [
            {"post_id": 401, "topic": "other", "importance": 3, "is_advertisement": False},
            {"post_id": 402, "topic": "A", "importance": 1, "is_advertisement": False},
            {"post_id": 403, "topic": "A", "importance": 4, "is_advertisement": True},
            {"post_id": 404, "topic": "B", "importance": 4, "is_advertisement": False},
        ]
    })
    ai_mock.generate_json_response = AsyncMock(return_value=ai_resp)

    res = await classifier.classify_posts(posts)
    assert res["total_classified"] == 1
    assert res["total_discarded"] == 3

    classified = res["classified_posts"]
    assert classified[0].id == 404
    assert classified[0].topic == "B"
    assert classified[0].status == "classified"

    discarded = {p.id: p for p in res["discarded_posts"]}
    assert "topic other" in discarded[401].discard_reason
    assert "importance 1" in discarded[402].discard_reason
    assert discarded[403].discard_reason == "advertisement"


@pytest.mark.asyncio
async def test_extracted_fields_for_topics_d_and_f():
    """Verifies structured extraction for Topic D and Topic F."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    posts = [
        DigestPost(id=501, chat_id=-100, message_id=20, text="AI Engineer vakansiyasi, $5000 oyiga"),
        DigestPost(id=502, chat_id=-100, message_id=21, text="TSUE talabalari uchun xalqaro grant, muddati 1-dekabr"),
    ]

    ai_resp = json.dumps({
        "items": [
            {
                "post_id": 501,
                "topic": "D",
                "importance": 5,
                "is_advertisement": False,
                "extracted": {
                    "profession": "AI Engineer",
                    "amount": "$5000",
                    "currency": "USD",
                    "period": "oyiga",
                    "location": "Masofaviy",
                    "employer": "OpenAI Partner",
                },
            },
            {
                "post_id": 502,
                "topic": "F",
                "importance": 5,
                "is_advertisement": False,
                "extracted": {
                    "title": "TSUE Xalqaro Granti",
                    "deadline": "1-dekabr",
                    "how_to_apply": "tsue.uz saytida ariza topshiring",
                },
            },
        ]
    })
    ai_mock.generate_json_response = AsyncMock(return_value=ai_resp)

    res = await classifier.classify_posts(posts)
    assert res["total_classified"] == 2

    p1 = [p for p in res["classified_posts"] if p.id == 501][0]
    ext1 = json.loads(p1.extracted_json)
    assert ext1["profession"] == "AI Engineer"
    assert ext1["amount"] == "$5000"

    p2 = [p for p in res["classified_posts"] if p.id == 502][0]
    ext2 = json.loads(p2.extracted_json)
    assert ext2["title"] == "TSUE Xalqaro Granti"
    assert ext2["deadline"] == "1-dekabr"


@pytest.mark.asyncio
async def test_quota_limits_and_skipped_posts():
    """Verifies MAX_AI_CALLS_PER_RUN quota limit and tracking of skipped posts."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    # 15 posts total
    posts = [
        DigestPost(id=i, chat_id=-100, message_id=i, text=f"Yangilik {i}", chat_username=f"channel_{i}")
        for i in range(1, 16)
    ]

    # Patch settings: batch_size=5, max_calls=2 -> max 10 posts can be processed, 5 skipped
    with patch("services.digest_classifier.settings.BATCH_SIZE", 5), \
         patch("services.digest_classifier.settings.MAX_AI_CALLS_PER_RUN", 2):

        def fake_response(prompt, temperature=0.0):
            # Parse post ids in prompt
            import re
            ids = [int(m) for m in re.findall(r"\[POST ID: (\d+)\]", prompt)]
            items = [{"post_id": pid, "topic": "E", "importance": 4, "is_advertisement": False} for pid in ids]
            return json.dumps({"items": items})

        ai_mock.generate_json_response = AsyncMock(side_effect=fake_response)

        res = await classifier.classify_posts(posts)
        assert res["ai_calls_made"] == 2
        assert res["skipped_posts_count"] == 5
        assert res["total_classified"] == 10
        assert res["total_discarded"] == 5

        # Check skipped posts discard reason
        for dp in res["discarded_posts"]:
            assert dp.discard_reason == "quota_limit_skipped"


@pytest.mark.asyncio
async def test_digest_service_classification_pipeline(temp_db_crud):
    """End-to-end integration test of DigestService.classify_run_posts."""
    crud, _ = temp_db_crud
    ai_mock = MagicMock(spec=AIEngine)

    service = DigestService(crud, ai_mock)

    # Create posts in DB with status='new'
    p1 = DigestPost(chat_id=-1001, message_id=1, text="Anthropic yangi Claude modelini chiqardi", status="new")
    p2 = DigestPost(chat_id=-1001, message_id=2, text="Umumiy salomlar", status="new")
    id1 = await crud.save_digest_post(p1)
    id2 = await crud.save_digest_post(p2)

    ai_resp = json.dumps({
        "items": [
            {"post_id": id1, "topic": "E", "importance": 5, "is_advertisement": False},
            {"post_id": id2, "topic": "other", "importance": 1, "is_advertisement": False},
        ]
    })
    ai_mock.generate_json_response = AsyncMock(return_value=ai_resp)

    stats = await service.classify_run_posts()
    assert stats["total_classified"] == 1
    assert stats["total_discarded"] == 1
    assert stats["topic_counts"].get("E") == 1

    # Verify database state
    db_posts = await crud.get_digest_posts(status="classified")
    assert len(db_posts) == 1
    assert db_posts[0].id == id1
    assert db_posts[0].topic == "E"
    assert db_posts[0].importance == 5

    db_discarded = await crud.get_digest_posts(status="discarded")
    assert len(db_discarded) == 1
    assert db_discarded[0].id == id2


def test_parse_json_array_without_items_wrapper():
    """Verifies classifier handles top-level JSON list [ {...} ] without items wrapper."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    raw_array = json.dumps([
        {"post_id": 10, "topic": "A", "importance": 4, "is_advertisement": False},
        {"post_id": 11, "topic": "E", "importance": 5, "is_advertisement": False},
    ])
    res = classifier.parse_and_validate_response(raw_array, {10, 11})
    assert res is not None
    assert len(res) == 2
    assert res[0].post_id == 10
    assert res[0].topic == "A"
    assert res[1].post_id == 11
    assert res[1].topic == "E"


def test_parse_json_with_alternative_keys():
    """Verifies classifier handles alternative top-level keys like 'posts' or 'results'."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    raw_posts = json.dumps({
        "posts": [
            {"post_id": 20, "topic": "B", "importance": 3, "is_advertisement": False},
        ]
    })
    res = classifier.parse_and_validate_response(raw_posts, {20})
    assert res is not None
    assert len(res) == 1
    assert res[0].post_id == 20
    assert res[0].topic == "B"


@pytest.mark.asyncio
async def test_topic_normalization_and_importance_clamping():
    """Verifies that descriptive topic strings ('A. Siyosat', 'Topic E') normalize cleanly and out-of-range importance is clamped."""
    ai_mock = MagicMock(spec=AIEngine)
    classifier = DigestClassifier(ai_mock)

    posts = [
        DigestPost(id=1, chat_id=-1, message_id=1, text="Prezident farmoni qabul qilindi", status="new"),
        DigestPost(id=2, chat_id=-1, message_id=2, text="Yangi AI algoritmi ishga tushirildi", status="new"),
    ]

    raw_resp = json.dumps({
        "items": [
            {"post_id": 1, "topic": "A. Siyosat va qonunchilik", "importance": 6, "is_advertisement": False},
            {"post_id": 2, "topic": "E - Sun'iy intellekt", "importance": 0, "is_advertisement": False},
        ]
    })
    ai_mock.generate_json_response = AsyncMock(return_value=raw_resp)

    res = await classifier.classify_posts(posts)
    # Post 1: topic "A", importance clamped to 5 -> classified
    assert res["total_classified"] >= 1
    classified_p1 = [p for p in res["classified_posts"] if p.id == 1]
    assert len(classified_p1) == 1
    assert classified_p1[0].topic == "A"
    assert classified_p1[0].importance == 5

    # Post 2: topic "E", importance clamped to 1 -> discarded because min_importance=2
    discarded_p2 = [p for p in res["discarded_posts"] if p.id == 2]
    assert len(discarded_p2) == 1
    assert discarded_p2[0].topic == "E"
    assert discarded_p2[0].importance == 1

