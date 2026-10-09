"""
Memory Embedding Service for Antigravity Butler.
Handles Gemini embedding generation, off-thread asynchronous dispatch,
strict timeout enforcement, daily quota tracking, vector BLOB serialization,
and cosine similarity computation.
"""

import asyncio
import logging
from typing import List, Optional, Sequence, Union
import numpy as np

try:
    from google import genai
    NEW_SDK = True
except ImportError:
    import google.generativeai as genai
    NEW_SDK = False

from config import settings
from database.crud import DatabaseManager

logger = logging.getLogger(__name__)


def vector_to_blob(vector: Sequence[float]) -> bytes:
    """Serializes a float vector into a 32-bit float binary BLOB."""
    return np.asarray(vector, dtype=np.float32).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    """Deserializes a 32-bit float binary BLOB into a NumPy array."""
    return np.frombuffer(blob, dtype=np.float32)


def cosine_similarity(
    vec_a: Union[Sequence[float], np.ndarray],
    vec_b: Union[Sequence[float], np.ndarray],
) -> float:
    """
    Computes cosine similarity between two vectors.
    Returns 0.0 if either vector has near-zero magnitude.
    """
    a = np.asarray(vec_a, dtype=np.float32)
    b = np.asarray(vec_b, dtype=np.float32)
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm <= 1e-9:
        return 0.0
    return float(np.dot(a, b) / norm)


class MemoryEmbeddingService:
    """
    Service for generating and managing vector embeddings for Contact Memory.
    Adheres to Fail-Open (P15): on failure or rate-limit exhaustion, returns None.
    """

    def __init__(
        self,
        db_manager: DatabaseManager,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        dim: Optional[int] = None,
    ):
        self.db_manager = db_manager
        self.api_key = api_key if api_key is not None else settings.GEMINI_API_KEY
        self.model_name = model_name or settings.MEM_EMBEDDING_MODEL
        self.dim = dim or settings.MEM_EMBEDDING_DIM
        self._client = None
        self._init_client()

    def _init_client(self) -> None:
        """Initializes the Gemini client if a valid API key is present."""
        if not self.api_key or self.api_key.startswith("AIzaSyYour"):
            logger.warning("No valid Gemini API key configured for MemoryEmbeddingService.")
            return

        try:
            if NEW_SDK:
                self._client = genai.Client(api_key=self.api_key)
            else:
                genai.configure(api_key=self.api_key)
                self._client = genai
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client for embeddings: {e}")
            self._client = None

    async def embed_text(self, text: str) -> Optional[List[float]]:
        """
        Generates an embedding vector for the given text using Gemini.
        Executes off-thread via asyncio.to_thread with strict timeout.
        Tracks daily quota against MEM_MAX_EMBED_CALLS_PER_DAY.
        Returns None on any error or quota exhaustion (fail-open).
        """
        if not text or not text.strip():
            return None

        # Check daily quota
        daily_calls = await self.db_manager.get_daily_counter("embed")
        max_daily = getattr(settings, "MEM_MAX_EMBED_CALLS_PER_DAY", 200)
        if daily_calls >= max_daily:
            logger.warning(
                f"Embedding daily quota reached ({daily_calls}/{max_daily}). "
                f"Skipping API call and failing open to keyword search."
            )
            return None

        if self._client is None:
            logger.debug("Gemini client not initialized for embeddings.")
            return None

        timeout_sec = getattr(settings, "MEM_EMBEDDING_TIMEOUT_SECONDS", 15.0)

        def _sync_embed() -> Optional[List[float]]:
            if NEW_SDK:
                response = self._client.models.embed_content(
                    model=self.model_name,
                    contents=text,
                )
                if hasattr(response, "embeddings") and response.embeddings:
                    first = response.embeddings[0]
                    if hasattr(first, "values"):
                        return list(first.values)
                return None
            else:
                result = genai.embed_content(
                    model=f"models/{self.model_name}",
                    content=text,
                    task_type="retrieval_document",
                )
                if isinstance(result, dict) and "embedding" in result:
                    return list(result["embedding"])
                return None

        try:
            vector = await asyncio.wait_for(
                asyncio.to_thread(_sync_embed),
                timeout=timeout_sec,
            )
            if vector is not None:
                await self.db_manager.increment_daily_counter("embed")
                return vector
            return None
        except asyncio.TimeoutError:
            logger.warning(f"Embedding API call timed out after {timeout_sec}s.")
            return None
        except Exception as e:
            logger.warning(f"Embedding generation error: {e}")
            return None

    async def embed_and_save(
        self,
        contact_user_id: int,
        item_type: str,
        item_id: int,
        text: str,
    ) -> Optional[int]:
        """
        Generates embedding and saves to mem_embeddings table.
        Returns the saved embedding row ID or None if embedding failed.
        """
        vector = await self.embed_text(text)
        if not vector:
            return None

        blob = vector_to_blob(vector)
        row_id = await self.db_manager.save_mem_embedding(
            contact_user_id=contact_user_id,
            item_type=item_type,
            item_id=item_id,
            model_name=self.model_name,
            dim=len(vector),
            vector=blob,
        )
        return row_id
