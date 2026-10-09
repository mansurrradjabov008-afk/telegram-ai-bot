"""
Unit and integration test suite for Contact Memory Phase 2: Retrieval Foundation.
Covers:
- Vector BLOB serialization/deserialization with float32 precision
- Cosine similarity calculation (identical, orthogonal, opposite, zero-norm)
- SQLite FTS5 virtual tables and sync triggers for messages and facts
- SQLite FTS5 BM25 search with query token sanitization (hyphens, punctuation)
- Strict per-contact isolation at SQL level and hard guardrail assertion (H4)
- Unified MemoryRetriever primary vector path vs FTS5 BM25 fallback path
- Model version awareness (ignoring embeddings from older models)
- Daily embedding API quota rate-limiting (fail-open)
- Background pending embeddings worker processing
- Feature flag (MEMORY_ENABLED=False) and paused contact handling
"""

import asyncio
from pathlib import Path
from typing import List, Optional
import numpy as np
import pytest
import pytest_asyncio

from config import settings
from database.connection import init_db, get_db
from database.crud import DatabaseManager
from database.models import MemEmbedding
from services.memory_embedding import (
    MemoryEmbeddingService,
    vector_to_blob,
    blob_to_vector,
    cosine_similarity,
)
from services.memory_retriever import MemoryRetriever
from services.memory_service import MemoryService


@pytest_asyncio.fixture
async def temp_db(tmp_path: Path):
    """Provides a fresh isolated temporary SQLite database for Phase 2 retrieval testing."""
    temp_path = tmp_path / "test_retrieval_p2.db"
    await init_db(temp_path)
    db_mgr = DatabaseManager(temp_path)
    # Seed sample CRM contacts
    await db_mgr.upsert_contact(user_id=101, full_name="Sherzod Saidov", username="sherzod", mode="draft")
    await db_mgr.upsert_contact(user_id=102, full_name="Dilshod Karimov", username="dilshod", mode="auto")
    await db_mgr.bind_mem_contact(101, "sherzod")
    await db_mgr.bind_mem_contact(102, "dilshod")
    return db_mgr, temp_path


class TestVectorMathAndSerialization:
    """Verifies vector serialization and cosine similarity computation."""

    def test_vector_blob_roundtrip(self):
        rng = np.random.default_rng(42)
        original = rng.normal(size=3072).tolist()
        blob = vector_to_blob(original)
        assert len(blob) == 3072 * 4  # 3072 * 4 bytes for float32

        recovered = blob_to_vector(blob)
        assert len(recovered) == 3072
        np.testing.assert_allclose(original, recovered, rtol=1e-5, atol=1e-6)

    def test_cosine_similarity_properties(self):
        v1 = [1.0, 0.0, 0.0]
        v2 = [1.0, 0.0, 0.0]
        v_ortho = [0.0, 1.0, 0.0]
        v_opp = [-1.0, 0.0, 0.0]
        v_zero = [0.0, 0.0, 0.0]

        assert abs(cosine_similarity(v1, v2) - 1.0) < 1e-5
        assert abs(cosine_similarity(v1, v_ortho)) < 1e-5
        assert abs(cosine_similarity(v1, v_opp) - (-1.0)) < 1e-5
        assert cosine_similarity(v1, v_zero) == 0.0


class TestFTS5VirtualTablesAndTriggers:
    """Verifies SQLite FTS5 triggers, sync, and BM25 ranking."""

    @pytest.mark.asyncio
    async def test_fts5_virtual_tables_and_triggers_sync(self, temp_db):
        db_mgr, _ = temp_db
        # Insert a message into mem_messages
        msg_id = await db_mgr.log_mem_message(
            contact_user_id=101,
            tg_message_id=501,
            sender="contact",
            text="Toshkent shahrida ob-havo ertaga juda issiq bo'ladi",
        )
        assert msg_id is not None

        # Verify FTS5 search finds it
        res = await db_mgr.search_mem_messages_fts(101, "ob-havo issiq")
        assert len(res) == 1
        assert res[0]["id"] == msg_id
        assert "Toshkent" in res[0]["text_redacted"]

        # Update message
        await db_mgr.update_mem_message_edit(101, 501, "Samarqand shahrida yomg'ir yog'moqda")
        res_old = await db_mgr.search_mem_messages_fts(101, "issiq")
        assert len(res_old) == 0

        res_new = await db_mgr.search_mem_messages_fts(101, "Samarqand yomg'ir")
        assert len(res_new) == 1
        assert "Samarqand" in res_new[0]["text_redacted"]

    @pytest.mark.asyncio
    async def test_fts5_facts_status_filter(self, temp_db):
        db_mgr, _ = temp_db
        # Create active fact
        f1_id = await db_mgr.create_mem_fact(
            contact_user_id=101,
            category="location",
            text="Toshkentda IT kompaniyada ishlaydi",
            source_type="contact_claim",
        )
        # Create another fact and mark as superseded
        f2_id = await db_mgr.create_mem_fact(
            contact_user_id=101,
            category="location",
            text="Oldin Samarqandda yashagan",
            source_type="contact_claim",
        )
        async with db_mgr.serialized_transaction() as db:
            await db.execute("UPDATE mem_facts SET status = 'superseded' WHERE id = ?", (f2_id,))

        # Search should only return active fact
        res = await db_mgr.search_mem_facts_fts(101, "kompaniyada ishlaydi")
        assert len(res) == 1
        assert res[0]["id"] == f1_id

        # Superseded fact should NOT appear
        res_super = await db_mgr.search_mem_facts_fts(101, "Samarqandda yashagan")
        assert len(res_super) == 0

    @pytest.mark.asyncio
    async def test_fts5_query_sanitization_special_characters(self, temp_db):
        db_mgr, _ = temp_db
        await db_mgr.log_mem_message(
            contact_user_id=101,
            tg_message_id=502,
            sender="contact",
            text="Loyihaning narxi 100% kelishilgan, ertaga ko'rishamiz!",
        )

        # Query with hyphens, percent signs, question marks
        tricky_query = "narxi: 100% ob-havo? (ertaga)*"
        res = await db_mgr.search_mem_messages_fts(101, tricky_query)
        assert len(res) >= 1
        assert res[0]["tg_message_id"] == 502


class TestH4StrictIsolation:
    """Verifies that under no circumstances can contact A see contact B's data."""

    @pytest.mark.asyncio
    async def test_sql_isolation_between_contacts(self, temp_db):
        db_mgr, _ = temp_db
        # Message for 101
        await db_mgr.log_mem_message(101, 1, "contact", "Maxfiy loyiha A")
        # Message for 102
        await db_mgr.log_mem_message(102, 2, "contact", "Maxfiy loyiha B")

        # Query for 101 must NEVER return 102
        res_101 = await db_mgr.search_mem_messages_fts(101, "Maxfiy loyiha")
        assert len(res_101) == 1
        assert res_101[0]["contact_user_id"] == 101

        res_102 = await db_mgr.search_mem_messages_fts(102, "Maxfiy loyiha")
        assert len(res_102) == 1
        assert res_102[0]["contact_user_id"] == 102

    @pytest.mark.asyncio
    async def test_embeddings_isolation(self, temp_db):
        db_mgr, _ = temp_db
        v = [0.1] * 3072
        blob = vector_to_blob(v)
        await db_mgr.save_mem_embedding(101, "message", 1, "gemini-embedding-001", 3072, blob)
        await db_mgr.save_mem_embedding(102, "message", 2, "gemini-embedding-001", 3072, blob)

        embs_101 = await db_mgr.get_mem_embeddings_by_contact(101)
        assert len(embs_101) == 1
        assert embs_101[0].contact_user_id == 101

        embs_102 = await db_mgr.get_mem_embeddings_by_contact(102)
        assert len(embs_102) == 1
        assert embs_102[0].contact_user_id == 102

    @pytest.mark.asyncio
    async def test_retriever_hard_assertion_drops_on_breach(self, temp_db, monkeypatch):
        db_mgr, _ = temp_db
        retriever = MemoryRetriever(db_mgr)

        # Mock search_mem_messages_fts to simulate a malicious or buggy leak from another contact
        async def mock_leaky_search(*args, **kwargs):
            return [{
                "id": 999,
                "contact_user_id": 999,  # FOREIGN CONTACT ID!
                "tg_message_id": 1,
                "sender": "contact",
                "text_redacted": "Leaked message from stranger",
                "sent_at": "2026-10-09",
                "rank": -1.0,
            }]

        monkeypatch.setattr(db_mgr, "search_mem_messages_fts", mock_leaky_search)

        # Even if mock returns leaked data, retriever H4 guardrail MUST drop all and return empty
        results, path = await retriever.retrieve(101, "test query")
        assert results == []
        assert path == "isolation_violation_dropped"


class MockEmbeddingService(MemoryEmbeddingService):
    """Predictable mock embedding service for unit testing without live network calls."""

    def __init__(self, db_mgr, return_vec=None):
        super().__init__(db_mgr)
        self.return_vec = return_vec or ([0.5] * 3072)
        self.fail_mode = False

    async def embed_text(self, text: str) -> Optional[List[float]]:
        if self.fail_mode:
            return None
        await self.db_manager.increment_daily_counter("embed")
        return self.return_vec


class TestMemoryRetrieverIntegration:
    """Verifies unified retriever logic: vector search, model versioning, FTS5 fallback."""

    @pytest.mark.asyncio
    async def test_vector_primary_retrieval(self, temp_db):
        db_mgr, _ = temp_db
        mock_embed = MockEmbeddingService(db_mgr, return_vec=[1.0] + [0.0] * 3071)
        retriever = MemoryRetriever(db_mgr, embedding_service=mock_embed)

        # Log a message and save matching embedding
        msg_id = await db_mgr.log_mem_message(101, 10, "contact", "Bugun soat 5da uchrashamiz")
        blob = vector_to_blob([1.0] + [0.0] * 3071)
        await db_mgr.save_mem_embedding(101, "message", msg_id, settings.MEM_EMBEDDING_MODEL, 3072, blob)

        results, path = await retriever.retrieve(101, "uchrashuv vaqti")
        assert path == "vector"
        assert len(results) == 1
        assert results[0]["item_id"] == msg_id
        assert results[0]["score"] == 1.0
        assert results[0]["retrieval_mode"] == "vector"

    @pytest.mark.asyncio
    async def test_outdated_model_embeddings_ignored(self, temp_db):
        db_mgr, _ = temp_db
        mock_embed = MockEmbeddingService(db_mgr, return_vec=[1.0] + [0.0] * 3071)
        retriever = MemoryRetriever(db_mgr, embedding_service=mock_embed)

        msg_id = await db_mgr.log_mem_message(101, 11, "contact", "Eski embedding modeli bilan saqlangan")
        blob = vector_to_blob([1.0] + [0.0] * 3071)
        # Save with outdated model name
        await db_mgr.save_mem_embedding(101, "message", msg_id, "old-legacy-model-v0", 3072, blob)

        # Outdated model embedding should be ignored, falling back to FTS5
        results, path = await retriever.retrieve(101, "Eski embedding")
        assert path == "fts5_recency_fallback"
        assert len(results) == 1
        assert results[0]["item_id"] == msg_id
        assert results[0]["retrieval_mode"] == "fts5"

    @pytest.mark.asyncio
    async def test_low_similarity_below_threshold_ignored(self, temp_db):
        db_mgr, _ = temp_db
        # Query vector orthogonal to stored vector -> similarity 0.0 < threshold 0.40
        mock_embed = MockEmbeddingService(db_mgr, return_vec=[1.0] + [0.0] * 3071)
        retriever = MemoryRetriever(db_mgr, embedding_service=mock_embed)

        msg_id = await db_mgr.log_mem_message(101, 12, "contact", "Boshqa mavzu haqida")
        stored_vec = [0.0, 1.0] + [0.0] * 3070  # orthogonal
        await db_mgr.save_mem_embedding(101, "message", msg_id, settings.MEM_EMBEDDING_MODEL, 3072, vector_to_blob(stored_vec))

        # Because similarity is 0.0 (< 0.40), vector path returns nothing, falls back to FTS5 (which also won't match unrelated keywords)
        results, path = await retriever.retrieve(101, "Avtomobil narxlari")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_embedding_failure_falls_back_to_fts5(self, temp_db):
        db_mgr, _ = temp_db
        mock_embed = MockEmbeddingService(db_mgr)
        mock_embed.fail_mode = True  # Simulates network error, timeout, or quota exceeded
        retriever = MemoryRetriever(db_mgr, embedding_service=mock_embed)

        msg_id = await db_mgr.log_mem_message(101, 13, "contact", "Telegram bot arxitekturasi")

        results, path = await retriever.retrieve(101, "bot arxitekturasi")
        assert path == "fts5_recency_fallback"
        assert len(results) == 1
        assert results[0]["item_id"] == msg_id

    @pytest.mark.asyncio
    async def test_feature_flag_disabled(self, temp_db, monkeypatch):
        db_mgr, _ = temp_db
        monkeypatch.setattr(settings, "MEMORY_ENABLED", False)
        retriever = MemoryRetriever(db_mgr)

        results, path = await retriever.retrieve(101, "har qanday xabar")
        assert results == []
        assert path == "disabled"

    @pytest.mark.asyncio
    async def test_paused_contact_returns_paused(self, temp_db):
        db_mgr, _ = temp_db
        await db_mgr.pause_mem_contact(101, "username_changed")
        retriever = MemoryRetriever(db_mgr)

        results, path = await retriever.retrieve(101, "salom")
        assert results == []
        assert path == "contact_memory_paused"


class TestDailyQuotaAndBackgroundWorker:
    """Verifies quota management and background batch processing."""

    @pytest.mark.asyncio
    async def test_daily_embed_quota_enforcement(self, temp_db, monkeypatch):
        db_mgr, _ = temp_db
        # Set max calls to 2
        monkeypatch.setattr(settings, "MEM_MAX_EMBED_CALLS_PER_DAY", 2)
        service = MemoryEmbeddingService(db_mgr)

        # Mock _client
        class FakeClient:
            class models:
                @staticmethod
                def embed_content(model, contents):
                    class Resp:
                        class Embed:
                            values = [0.1] * 3072
                        embeddings = [Embed()]
                    return Resp()

        service._client = FakeClient()

        # Call 1
        v1 = await service.embed_text("xabar 1")
        assert v1 is not None
        # Call 2
        v2 = await service.embed_text("xabar 2")
        assert v2 is not None
        # Call 3 (Quota reached)
        v3 = await service.embed_text("xabar 3")
        assert v3 is None  # Fails open!

    @pytest.mark.asyncio
    async def test_background_worker_embeds_pending_items(self, temp_db):
        db_mgr, _ = temp_db
        mock_embed = MockEmbeddingService(db_mgr)
        mem_service = MemoryService(db_mgr, embedding_service=mock_embed)

        # Seed unembedded items
        f_id = await db_mgr.create_mem_fact(101, "hobby", "Futbol o'ynashni yoqtiradi", "contact_claim")
        m_id = await db_mgr.log_mem_message(101, 100, "contact", "Ertaga soat 10da kelaman")

        # Before processing: 0 embeddings
        embs_before = await db_mgr.get_mem_embeddings_by_contact(101)
        assert len(embs_before) == 0

        # Process pending
        processed = await mem_service.process_pending_embeddings(limit=10)
        assert processed == 2

        # After processing: 2 embeddings stored in DB
        embs_after = await db_mgr.get_mem_embeddings_by_contact(101)
        assert len(embs_after) == 2
        item_types = {e.item_type for e in embs_after}
        assert item_types == {"fact", "message"}
