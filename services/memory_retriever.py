"""
Memory Retrieval Engine for Antigravity Butler.
Coordinates semantic vector search with cosine similarity, SQLite FTS5 keyword
search with BM25 ranking fallback, model version validation, and strict per-contact
isolation enforcement (H4).
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from config import settings
from database.crud import DatabaseManager
from services.memory_embedding import MemoryEmbeddingService, blob_to_vector, cosine_similarity

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """
    Unified retriever for contact memory.
    Ensures that for any reply to contact A, only data belonging to contact A is retrieved (H4).
    Supports vector search with automatic fallback to FTS5 BM25 keyword matching (P15).
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        embedding_service: Optional[MemoryEmbeddingService] = None,
    ):
        self.db_manager = db_manager
        self.embedding_service = embedding_service or MemoryEmbeddingService(db_manager=db_manager)

    async def retrieve(
        self,
        contact_user_id: int,
        query_text: str,
        k: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Retrieves relevant memory items (messages, facts) for target contact.
        Returns:
            (items, path_used) where path_used is 'vector', 'fts5_recency_fallback',
            'empty', 'disabled', or 'contact_memory_paused'.
        """
        if not getattr(settings, "MEMORY_ENABLED", True):
            return [], "disabled"

        # Check contact memory status
        contact = await self.db_manager.get_mem_contact(contact_user_id)
        if contact and contact.memory_enabled == 0:
            logger.debug(f"Memory is paused for user {contact_user_id}: {contact.paused_reason}")
            return [], "contact_memory_paused"

        limit_k = k or getattr(settings, "MEM_RETRIEVAL_TOP_K", 5)
        results: List[Dict[str, Any]] = []
        path_used = "empty"

        # ----------------------------------------------------
        # 1. PRIMARY PATH: Vector Semantic Search
        # ----------------------------------------------------
        query_vector = None
        if self.embedding_service:
            try:
                query_vector = await self.embedding_service.embed_text(query_text)
            except Exception as e:
                logger.warning(f"Failed to generate query embedding: {type(e).__name__}")
                query_vector = None

        if query_vector is not None:
            try:
                # Fetch stored embeddings strictly for this contact (H4 SQL filter)
                stored_embeddings = await self.db_manager.get_mem_embeddings_by_contact(contact_user_id)
                current_model = getattr(self.embedding_service, "model_name", settings.MEM_EMBEDDING_MODEL)
                threshold = getattr(settings, "MEM_SIMILARITY_THRESHOLD", 0.40)

                scored_items = []
                for emb in stored_embeddings:
                    # Model version awareness: ignore outdated model embeddings
                    if emb.model_name != current_model:
                        continue

                    cand_vector = blob_to_vector(emb.vector)
                    sim = cosine_similarity(query_vector, cand_vector)
                    if sim >= threshold:
                        scored_items.append((sim, emb))

                # Sort by similarity descending
                scored_items.sort(key=lambda x: x[0], reverse=True)
                top_candidates = scored_items[:limit_k]

                # Hydrate underlying records
                for sim, emb in top_candidates:
                    if emb.item_type == "message":
                        msg = await self.db_manager.get_mem_message_by_id(contact_user_id, emb.item_id)
                        if msg:
                            results.append({
                                "item_type": "message",
                                "item_id": msg.id,
                                "contact_user_id": msg.contact_user_id,
                                "text": msg.text_redacted,
                                "sender": msg.sender,
                                "sent_at": msg.sent_at,
                                "score": round(sim, 4),
                                "retrieval_mode": "vector",
                            })
                    elif emb.item_type == "fact":
                        f = await self.db_manager.get_mem_fact_by_id(contact_user_id, emb.item_id)
                        if f and f.status == "active":
                            results.append({
                                "item_type": "fact",
                                "item_id": f.id,
                                "contact_user_id": f.contact_user_id,
                                "category": f.category,
                                "text": f.text,
                                "source_type": f.source_type,
                                "confidence": f.confidence,
                                "score": round(sim, 4),
                                "retrieval_mode": "vector",
                            })

                if results:
                    path_used = "vector"
            except Exception as e:
                logger.warning(f"Vector retrieval failed for user {contact_user_id}: {type(e).__name__}")
                results = []

        # ----------------------------------------------------
        # 2. FALLBACK PATH: SQLite FTS5 BM25 Keyword Search
        # ----------------------------------------------------
        if not results:
            try:
                fts_messages = await self.db_manager.search_mem_messages_fts(
                    contact_user_id=contact_user_id,
                    query_text=query_text,
                    limit=limit_k,
                )
                for m in fts_messages:
                    results.append({
                        "item_type": "message",
                        "item_id": m["id"],
                        "contact_user_id": m["contact_user_id"],
                        "text": m["text_redacted"],
                        "sender": m["sender"],
                        "sent_at": m["sent_at"],
                        # In FTS5 BM25, lower is better; invert for uniform score
                        "score": round(-float(m.get("rank", 0.0)), 4),
                        "retrieval_mode": "fts5",
                    })

                fts_facts = await self.db_manager.search_mem_facts_fts(
                    contact_user_id=contact_user_id,
                    query_text=query_text,
                    limit=limit_k,
                )
                for f in fts_facts:
                    results.append({
                        "item_type": "fact",
                        "item_id": f["id"],
                        "contact_user_id": f["contact_user_id"],
                        "category": f.get("category"),
                        "text": f["text"],
                        "source_type": f["source_type"],
                        "confidence": f.get("confidence", 1.0),
                        "score": round(-float(f.get("rank", 0.0)), 4),
                        "retrieval_mode": "fts5",
                    })

                if results:
                    results.sort(key=lambda x: x["score"], reverse=True)
                    results = results[:limit_k]
                    path_used = "fts5_recency_fallback"
            except Exception as e:
                logger.warning(f"FTS5 retrieval failed for user {contact_user_id}: {type(e).__name__}")
                results = []

        # ----------------------------------------------------
        # 3. STRICT HARD ISOLATION ENFORCEMENT (H4)
        # ----------------------------------------------------
        for item in results:
            if item.get("contact_user_id") != contact_user_id:
                logger.critical(
                    f"🚨 [H4 SECURITY BREACH] Memory isolation failure detected! "
                    f"Target contact: {contact_user_id}, but found item belonging to {item.get('contact_user_id')}. "
                    f"Dropping all memory results immediately!"
                )
                return [], "isolation_violation_dropped"

        return results, path_used
