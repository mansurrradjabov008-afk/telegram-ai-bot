"""
Asynchronous Database Manager and CRUD operations for Telegram AI Butler.
"""

from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
import json
import re
import asyncio
from contextlib import asynccontextmanager
import aiosqlite
from .connection import get_db
from .models import (
    Contact,
    ScheduledTask,
    ConversationMessage,
    MonitoredSource,
    Draft,
    SchemaVersion,
    DigestSource,
    DigestPost,
    DigestRun,
    DigestState,
    MemContact,
    MemMessage,
    MemFact,
    MemOpenItem,
    MemSummary,
    MemEmbedding,
)
from services.memory_redactor import redact_text
from services.memory_categories import normalize_category


class DatabaseManager:
    _locks_by_loop: Dict[Any, asyncio.Lock] = {}

    def _get_write_lock(self) -> asyncio.Lock:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.Lock()
        if loop not in self._locks_by_loop:
            self._locks_by_loop[loop] = asyncio.Lock()
        return self._locks_by_loop[loop]

    @property
    def _write_lock(self) -> asyncio.Lock:
        return self._get_write_lock()

    def __init__(self, db_path=None):
        self.db_path = db_path

    @asynccontextmanager
    async def serialized_transaction(self):
        """
        Provides a centralized serialized SQLite write transaction protected by asyncio.Lock.
        Guarantees zero database-is-locked errors under high write concurrency.
        """
        async with self._get_write_lock():
            async with get_db(self.db_path) as db:
                yield db
                await db.commit()


    # ------------------ CONTACT CRM ------------------
    async def upsert_contact(
        self,
        user_id: int,
        full_name: str,
        username: Optional[str] = None,
        relationship_type: str = "general",
        addressing_style: str = "siz",
        mode: str = "auto",
        custom_instructions: Optional[str] = None,
        language_code: str = "uz",
    ) -> Contact:
        """Add or update contact relationship metadata and autonomy mode."""
        if username and username.startswith("@"):
            username = username[1:]

        if mode not in ("auto", "draft", "mute"):
            mode = "auto"

        query = """
        INSERT INTO contacts (user_id, username, full_name, relationship_type, addressing_style, mode, custom_instructions, language_code, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username=COALESCE(excluded.username, contacts.username),
            full_name=excluded.full_name,
            relationship_type=excluded.relationship_type,
            addressing_style=excluded.addressing_style,
            mode=COALESCE(excluded.mode, contacts.mode),
            custom_instructions=COALESCE(excluded.custom_instructions, contacts.custom_instructions),
            language_code=excluded.language_code,
            updated_at=CURRENT_TIMESTAMP;
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                query,
                (user_id, username, full_name, relationship_type, addressing_style, mode, custom_instructions, language_code),
            )
            await db.commit()

        return await self.get_contact_by_id(user_id)

    async def update_contact_mode(self, user_id: int, mode: str) -> bool:
        """Update autonomy mode ('auto', 'draft', 'mute') for a contact by numeric user_id."""
        if mode not in ("auto", "draft", "mute"):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'auto', 'draft', or 'mute'.")
        query = "UPDATE contacts SET mode = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (mode, user_id))
            await db.commit()
            return cursor.rowcount > 0

    async def update_contact_mode_by_username(self, username: str, mode: str) -> bool:
        """Update autonomy mode ('auto', 'draft', 'mute') for a contact by @username."""
        if mode not in ("auto", "draft", "mute"):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'auto', 'draft', or 'mute'.")
        if username.startswith("@"):
            username = username[1:]
        query = "UPDATE contacts SET mode = ?, updated_at = CURRENT_TIMESTAMP WHERE LOWER(username) = LOWER(?)"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (mode, username))
            await db.commit()
            return cursor.rowcount > 0

    async def update_all_contacts_mode(self, mode: str) -> int:
        """Update autonomy mode ('auto', 'draft', 'mute') for ALL contacts in CRM."""
        if mode not in ("auto", "draft", "mute"):
            raise ValueError(f"Invalid mode '{mode}'. Must be 'auto', 'draft', or 'mute'.")
        query = "UPDATE contacts SET mode = ?, updated_at = CURRENT_TIMESTAMP"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (mode,))
            await db.commit()
            return cursor.rowcount

    async def get_mode_counts(self) -> Dict[str, int]:
        """Fetch contact counts grouped by autonomy mode ('auto', 'draft', 'mute')."""
        query = "SELECT mode, COUNT(*) as cnt FROM contacts GROUP BY mode"
        counts = {"auto": 0, "draft": 0, "mute": 0}
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    m = r["mode"]
                    if m in counts:
                        counts[m] = r["cnt"]
        counts["total"] = counts["auto"] + counts["draft"] + counts["mute"]
        return counts

    async def update_addressing_style(self, user_id: int, addressing_style: str) -> bool:
        """Update only the addressing style ('siz' or 'sen') for a contact."""
        query = "UPDATE contacts SET addressing_style = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (addressing_style, user_id))
            await db.commit()
            return True

    async def delete_contact(self, user_id: int) -> bool:
        """Delete contact from CRM database."""
        query = "DELETE FROM contacts WHERE user_id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (user_id,))
            await db.commit()
            return True

    async def get_contact_by_id(self, user_id: int) -> Optional[Contact]:
        """Fetch contact profile by user_id."""
        query = "SELECT * FROM contacts WHERE user_id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Contact(**dict(row))

    async def get_contact_by_username(self, username: str) -> Optional[Contact]:
        """Fetch contact profile by @username."""
        if username.startswith("@"):
            username = username[1:]
        query = "SELECT * FROM contacts WHERE LOWER(username) = LOWER(?) ORDER BY updated_at DESC"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (username,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Contact(**dict(row))

    async def find_contact(self, query_str: str) -> Optional[Contact]:
        """
        Flexibly find a contact by:
        1. numeric user_id
        2. username (with or without '@', case-insensitive)
        3. full_name (case-insensitive exact or partial match)
        """
        if not query_str or not isinstance(query_str, str):
            return None
        cleaned = query_str.strip()
        if not cleaned:
            return None

        # 1. By numeric user_id
        if cleaned.isdigit():
            c = await self.get_contact_by_id(int(cleaned))
            if c:
                return c

        # 2. By username (case-insensitive)
        uname = cleaned.lstrip("@").strip()
        c = await self.get_contact_by_username(uname)
        if c:
            return c

        # 3. By full_name exact match (case-insensitive)
        query = "SELECT * FROM contacts WHERE LOWER(full_name) = LOWER(?) ORDER BY updated_at DESC"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (cleaned,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return Contact(**dict(row))

        # 4. By full_name partial match (case-insensitive)
        query = "SELECT * FROM contacts WHERE LOWER(full_name) LIKE LOWER(?) ORDER BY updated_at DESC"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (f"%{cleaned}%",)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return Contact(**dict(row))

        return None

    async def list_contacts(self) -> List[Contact]:
        """List all configured contacts in CRM."""
        query = "SELECT * FROM contacts ORDER BY updated_at DESC"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [Contact(**dict(r)) for r in rows]

    # ------------------ SCHEDULED TASKS ------------------
    async def create_scheduled_task(
        self,
        target_peer: str,
        message_text: str,
        scheduled_time: str,
    ) -> ScheduledTask:
        """Schedule a message task."""
        query = """
        INSERT INTO scheduled_tasks (target_peer, message_text, scheduled_time, status)
        VALUES (?, ?, ?, 'pending')
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (target_peer, message_text, scheduled_time))
            task_id = cursor.lastrowid
            await db.commit()

        return ScheduledTask(
            id=task_id,
            target_peer=target_peer,
            message_text=message_text,
            scheduled_time=scheduled_time,
            status="pending",
        )

    async def get_pending_tasks(self) -> List[ScheduledTask]:
        """Fetch all tasks ready or waiting for execution."""
        query = "SELECT * FROM scheduled_tasks WHERE status = 'pending' ORDER BY scheduled_time ASC"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [ScheduledTask(**dict(r)) for r in rows]

    async def update_task_status(
        self,
        task_id: int,
        status: str,
        error_message: Optional[str] = None,
    ):
        """Update task outcome status."""
        query = "UPDATE scheduled_tasks SET status = ?, error_message = ? WHERE id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (status, error_message, task_id))
            await db.commit()

    async def get_task_by_id(self, task_id: int) -> Optional[ScheduledTask]:
        """Fetch single task by its ID."""
        query = "SELECT * FROM scheduled_tasks WHERE id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (task_id,)) as cursor:
                row = await cursor.fetchone()
                return ScheduledTask(**dict(row)) if row else None

    async def get_task_history(self, limit: int = 10) -> List[ScheduledTask]:
        """Fetch recent completed or cancelled tasks."""
        query = "SELECT * FROM scheduled_tasks WHERE status != 'pending' ORDER BY id DESC LIMIT ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [ScheduledTask(**dict(r)) for r in rows]

    async def delete_task(self, task_id: int) -> bool:
        """Permanently delete a scheduled task."""
        query = "DELETE FROM scheduled_tasks WHERE id = ?"
        async with get_db(self.db_path) as db:
            await db.execute(query, (task_id,))
            await db.commit()
            return True

    # ------------------ CONVERSATION HISTORY ------------------
    async def log_message(
        self,
        chat_id: int,
        sender_name: str,
        role: str,
        content: str,
    ):
        """Save chat turn into history with automatic redaction (H4)."""
        content_redacted = redact_text(content)
        query = """
        INSERT INTO conversation_history (chat_id, sender_name, role, content)
        VALUES (?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (chat_id, sender_name, role, content_redacted))
            await db.commit()

    async def get_recent_history(self, chat_id: int, limit: int = 10) -> List[ConversationMessage]:
        """Get the latest N messages from a conversation."""
        query = """
        SELECT * FROM conversation_history
        WHERE chat_id = ?
        ORDER BY id DESC LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (chat_id, limit)) as cursor:
                rows = await cursor.fetchall()
                messages = [ConversationMessage(**dict(r)) for r in rows]
                return list(reversed(messages))

    # ------------------ DIGEST / FEEDS ------------------
    async def save_feed_post(self, source_peer_id: int, source_title: str, message_id: int, post_text: str):
        query = """
        INSERT INTO raw_feed_posts (source_peer_id, source_title, message_id, post_text)
        VALUES (?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (source_peer_id, source_title, message_id, post_text))
            await db.commit()

    async def get_unsummarized_posts(self, limit: int = 50) -> List[dict]:
        query = """
        SELECT * FROM raw_feed_posts
        WHERE is_summarized = 0
        ORDER BY id ASC LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (limit,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def mark_posts_summarized(self, post_ids: List[int]):
        if not post_ids:
            return
        placeholders = ",".join("?" for _ in post_ids)
        query = f"UPDATE raw_feed_posts SET is_summarized = 1 WHERE id IN ({placeholders})"
        async with get_db(self.db_path) as db:
            await db.execute(query, post_ids)
            await db.commit()

    async def get_recent_posts(self, limit: int = 30) -> List[dict]:
        """Fetch the most recent posts regardless of summarized status."""
        query = """
        SELECT * FROM raw_feed_posts
        ORDER BY id DESC LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (limit,)) as cursor:
                rows = await cursor.fetchall()
                # Return in chronological order
                return list(reversed([dict(r) for r in rows]))

    async def get_feed_statistics(self) -> dict:
        """Returns statistical counts about collected feed posts."""
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT count(*) FROM raw_feed_posts") as cur:
                total_posts = (await cur.fetchone())[0]
            async with db.execute("SELECT count(*) FROM raw_feed_posts WHERE is_summarized = 0") as cur:
                unsummarized = (await cur.fetchone())[0]
            async with db.execute("SELECT count(DISTINCT source_title) FROM raw_feed_posts") as cur:
                unique_sources = (await cur.fetchone())[0]
            return {
                "total_posts": total_posts,
                "unsummarized": unsummarized,
                "unique_sources": unique_sources,
            }

    # ------------------ DRAFTS & AUTONOMY MODES ------------------
    async def create_draft(
        self,
        source_message_id: int,
        incoming_text: str,
        proposed_reply: str,
        expires_at: Optional[str] = None,
        contact_username: Optional[str] = None,
        contact_user_id: Optional[int] = None,
        is_sensitive: bool = False,
        sensitive_reason: Optional[str] = None,
    ) -> Optional[Draft]:
        """
        Creates a new pending draft.
        Enforces duplicate draft protection via DB unique index.
        Defaults expires_at to 24 hours from creation if not explicitly specified.
        Returns the created Draft object, or None if a duplicate draft already exists.
        """
        if contact_username and contact_username.startswith("@"):
            contact_username = contact_username[1:]

        if expires_at is None:
            expires_at = (datetime.now() + timedelta(hours=24)).isoformat()

        query = """
        INSERT INTO drafts (
            contact_username, contact_user_id, source_message_id,
            incoming_text, proposed_reply, status, is_sensitive,
            sensitive_reason, expires_at
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            try:
                cursor = await db.execute(
                    query,
                    (
                        contact_username,
                        contact_user_id,
                        source_message_id,
                        incoming_text,
                        proposed_reply,
                        1 if is_sensitive else 0,
                        sensitive_reason,
                        expires_at,
                    ),
                )
                draft_id = cursor.lastrowid
                await db.commit()
                return await self.get_draft_by_id(draft_id)
            except aiosqlite.IntegrityError:
                # Prevent duplicate draft for the same contact message
                return None

    async def get_draft_by_id(self, draft_id: int) -> Optional[Draft]:
        """
        Fetch draft record by primary key ID.
        Returns Draft object if found, otherwise None.
        """
        query = "SELECT * FROM drafts WHERE id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (draft_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                data = dict(row)
                data["is_sensitive"] = bool(data.get("is_sensitive", 0))
                return Draft(**data)

    async def list_pending_drafts(self, limit: int = 20) -> List[Draft]:
        """
        Fetch pending, unexpired drafts ordered chronologically by created_at DESC.
        """
        now_iso = datetime.now().isoformat()
        query = """
        SELECT * FROM drafts 
        WHERE status = 'pending' AND expires_at > ? 
        ORDER BY created_at DESC 
        LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (now_iso, limit)) as cursor:
                rows = await cursor.fetchall()
                drafts = []
                for r in rows:
                    data = dict(r)
                    data["is_sensitive"] = bool(data.get("is_sensitive", 0))
                    drafts.append(Draft(**data))
                return drafts

    async def count_pending_drafts(self) -> int:
        """
        Counts active pending, unexpired drafts.
        """
        now_iso = datetime.now().isoformat()
        query = "SELECT COUNT(*) as cnt FROM drafts WHERE status = 'pending' AND expires_at > ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (now_iso,)) as cursor:
                row = await cursor.fetchone()
                return row["cnt"] if row else 0

    async def resolve_draft_atomic(
        self,
        draft_id: int,
        new_status: str,
        edited_reply: Optional[str] = None,
    ) -> bool:
        """
        Atomically transitions a pending draft to new_status ('sent', 'rejected', 'edited_sent', 'superseded', 'expired').
        Guarantees concurrency and double-press safety.
        Returns True if exactly one row transitioned, False otherwise.
        """
        if edited_reply is not None:
            query = """
            UPDATE drafts 
            SET status = ?, proposed_reply = ?, resolved_at = CURRENT_TIMESTAMP 
            WHERE id = ? AND status = 'pending'
            """
            params = (new_status, edited_reply, draft_id)
        else:
            query = """
            UPDATE drafts 
            SET status = ?, resolved_at = CURRENT_TIMESTAMP 
            WHERE id = ? AND status = 'pending'
            """
            params = (new_status, draft_id)

        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.rowcount == 1

    async def expire_old_drafts(self) -> int:
        """
        Marks pending drafts older than their expires_at timestamp as 'expired'.
        Returns count of expired drafts.
        """
        now_iso = datetime.now().isoformat()
        query = """
        UPDATE drafts 
        SET status = 'expired', resolved_at = CURRENT_TIMESTAMP 
        WHERE status = 'pending' AND expires_at <= ?
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (now_iso,))
            await db.commit()
            return cursor.rowcount

    async def get_schema_version(self) -> int:
        """
        Returns the current maximum schema version from schema_versions table.
        """
        query = "SELECT MAX(version) as max_v FROM schema_versions"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                row = await cursor.fetchone()
                return row["max_v"] if row and row["max_v"] is not None else 0

    # ------------------ SMART DIGEST V2 ------------------
    async def add_digest_source(
        self,
        peer_id: int,
        title: str,
        peer_type: str = "channel",
        username: Optional[str] = None,
        priority: int = 1,
    ) -> DigestSource:
        """Add or update a whitelisted source for Smart Digest V2."""
        if username and username.startswith("@"):
            username = username[1:]

        query = """
        INSERT INTO digest_sources (peer_id, title, username, peer_type, priority, is_active, added_at)
        VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(peer_id) DO UPDATE SET
            title = excluded.title,
            username = COALESCE(excluded.username, digest_sources.username),
            peer_type = excluded.peer_type,
            priority = excluded.priority,
            is_active = 1;
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (peer_id, title, username, peer_type, priority))
            await db.commit()

        source = await self.get_digest_source_by_peer_id(peer_id)
        if not source:
            raise RuntimeError(f"Failed to retrieve added digest source for peer_id {peer_id}")
        return source

    upsert_digest_source = add_digest_source

    async def remove_digest_source(self, identifier: Any) -> bool:
        """Remove a source from Smart Digest V2 whitelist by peer_id or username."""
        async with get_db(self.db_path) as db:
            if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.lstrip("-").isdigit()):
                peer_id = int(identifier)
                cursor = await db.execute("DELETE FROM digest_sources WHERE peer_id = ?", (peer_id,))
            else:
                clean_name = str(identifier).lstrip("@").strip()
                cursor = await db.execute("DELETE FROM digest_sources WHERE LOWER(username) = LOWER(?)", (clean_name,))
            await db.commit()
            return cursor.rowcount > 0

    async def list_digest_sources(self, active_only: bool = True) -> List[DigestSource]:
        """List whitelisted sources for Smart Digest V2."""
        query = "SELECT * FROM digest_sources"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY priority DESC, added_at ASC"

        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [DigestSource(**dict(r)) for r in rows]

    async def get_digest_source_by_peer_id(self, peer_id: int) -> Optional[DigestSource]:
        """Fetch digest source by numeric peer_id."""
        query = "SELECT * FROM digest_sources WHERE peer_id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (peer_id,)) as cursor:
                row = await cursor.fetchone()
                return DigestSource(**dict(row)) if row else None

    async def get_digest_source_by_username(self, username: str) -> Optional[DigestSource]:
        """Fetch digest source by username (case-insensitive)."""
        clean_name = username.lstrip("@").strip()
        query = "SELECT * FROM digest_sources WHERE LOWER(username) = LOWER(?)"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (clean_name,)) as cursor:
                row = await cursor.fetchone()
                return DigestSource(**dict(row)) if row else None

    async def get_digest_source_post_count(self, chat_id: int) -> int:
        """Counts collected posts for a specific source."""
        query = "SELECT COUNT(*) as cnt FROM digest_posts WHERE chat_id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (chat_id,)) as cursor:
                row = await cursor.fetchone()
                return row["cnt"] if row else 0

    async def get_all_source_post_counts(self) -> Dict[int, int]:
        """Returns mapping of chat_id to post count."""
        query = "SELECT chat_id, COUNT(*) as cnt FROM digest_posts GROUP BY chat_id"
        counts: Dict[int, int] = {}
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    counts[r["chat_id"]] = r["cnt"]
        return counts

    async def get_digest_state(self, chat_id: int) -> int:
        """Returns the last collected message_id for a source, or 0 if none."""
        query = "SELECT last_message_id FROM digest_state WHERE chat_id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (chat_id,)) as cursor:
                row = await cursor.fetchone()
                return row["last_message_id"] if row else 0

    async def set_digest_state(self, chat_id: int, last_message_id: int):
        """Updates the highest message_id collected for a source."""
        query = """
        INSERT INTO digest_state (chat_id, last_message_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_message_id = MAX(digest_state.last_message_id, excluded.last_message_id),
            updated_at = CURRENT_TIMESTAMP;
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (chat_id, last_message_id))
            await db.commit()

    async def save_digest_post(self, post: DigestPost) -> Optional[int]:
        """Saves a new digest post. Enforces uniqueness on (chat_id, message_id)."""
        query = """
        INSERT OR IGNORE INTO digest_posts (
            chat_id, message_id, chat_title, chat_username, text,
            original_source, posted_at, link, normalized_hash, fetched_at,
            spam_score, spam_reasons, topic, importance, status,
            discard_reason, digest_run_id, extracted_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                query,
                (
                    post.chat_id,
                    post.message_id,
                    post.chat_title,
                    post.chat_username,
                    post.text,
                    post.original_source,
                    post.posted_at,
                    post.link,
                    post.normalized_hash,
                    post.spam_score,
                    post.spam_reasons,
                    post.topic,
                    post.importance,
                    post.status,
                    post.discard_reason,
                    post.digest_run_id,
                    post.extracted_json,
                ),
            )
            await db.commit()
            return cursor.lastrowid if cursor.rowcount > 0 else None

    async def bulk_save_digest_posts(self, posts: List[DigestPost]) -> int:
        """Saves multiple digest posts in a single transaction."""
        if not posts:
            return 0
        query = """
        INSERT OR IGNORE INTO digest_posts (
            chat_id, message_id, chat_title, chat_username, text,
            original_source, posted_at, link, normalized_hash, fetched_at,
            spam_score, spam_reasons, topic, importance, status,
            discard_reason, digest_run_id, extracted_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = [
            (
                p.chat_id,
                p.message_id,
                p.chat_title,
                p.chat_username,
                p.text,
                p.original_source,
                p.posted_at,
                p.link,
                p.normalized_hash,
                p.spam_score,
                p.spam_reasons,
                p.topic,
                p.importance,
                p.status,
                p.discard_reason,
                p.digest_run_id,
                p.extracted_json,
            )
            for p in posts
        ]
        inserted = 0
        async with get_db(self.db_path) as db:
            for param in params:
                cur = await db.execute(query, param)
                if cur.rowcount > 0:
                    inserted += 1
            await db.commit()
        return inserted

    async def create_digest_run(self, trigger: str = "manual") -> int:
        """Creates a new digest run record and returns its ID."""
        query = "INSERT INTO digest_runs (trigger, status) VALUES (?, 'in_progress')"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (trigger,))
            await db.commit()
            return cursor.lastrowid

    async def finish_digest_run(
        self,
        run_id: int,
        status: str = "completed",
        stats: Optional[Dict[str, Any]] = None,
        duration_seconds: float = 0.0,
    ) -> bool:
        """Marks a digest run as finished and records stats JSON and telemetry metrics."""
        stats_json = json.dumps(stats, ensure_ascii=False, default=str) if stats else None

        posts_collected = 0
        posts_filtered = 0
        posts_classified = 0
        items_in_digest = 0
        ai_calls_count = 0

        if stats:
            posts_collected = stats.get("total_collected", 0)
            posts_filtered = stats.get("spam_filtered", 0) + stats.get("duplicates_filtered", 0)
            posts_classified = stats.get("classified_count", 0)
            items_in_digest = stats.get("total_items_selected", 0)
            ai_calls_count = (
                stats.get("classification", {}).get("ai_calls_made", 0)
                + stats.get("ai_calls_made", 0)
            )

        query = """
        UPDATE digest_runs 
        SET finished_at = CURRENT_TIMESTAMP,
            status = ?,
            stats_json = ?,
            posts_collected = ?,
            posts_filtered = ?,
            posts_classified = ?,
            items_in_digest = ?,
            ai_calls_count = ?,
            duration_seconds = ?
        WHERE id = ?
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                query,
                (
                    status,
                    stats_json,
                    posts_collected,
                    posts_filtered,
                    posts_classified,
                    items_in_digest,
                    ai_calls_count,
                    duration_seconds,
                    run_id,
                ),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_digest_stats_summary(self) -> Dict[str, Any]:
        """
        Calculates summary metrics for /digest stats:
        - Last run: time, posts collected, items delivered, spam dropped, ai calls, duration
        - Average run duration over last 7 runs
        - Total active sources count
        """
        async with get_db(self.db_path) as db:
            # 1. Last run
            last_run_query = """
            SELECT * FROM digest_runs 
            WHERE status IN ('completed', 'partial')
            ORDER BY id DESC LIMIT 1
            """
            async with db.execute(last_run_query) as cur:
                last_run_row = await cur.fetchone()
                last_run = dict(last_run_row) if last_run_row else None

            # 2. Last 7 runs duration
            durations_query = """
            SELECT duration_seconds FROM digest_runs 
            WHERE status IN ('completed', 'partial') AND duration_seconds > 0
            ORDER BY id DESC LIMIT 7
            """
            async with db.execute(durations_query) as cur:
                rows = await cur.fetchall()
                durations = [r["duration_seconds"] for r in rows if r["duration_seconds"]]
                avg_duration = sum(durations) / len(durations) if durations else 0.0

            # 3. Active sources count
            async with db.execute("SELECT COUNT(*) as cnt FROM digest_sources WHERE is_active = 1") as cur:
                src_row = await cur.fetchone()
                active_sources = src_row["cnt"] if src_row else 0

            # 4. Total runs count
            async with db.execute("SELECT COUNT(*) as cnt FROM digest_runs") as cur:
                total_runs_row = await cur.fetchone()
                total_runs = total_runs_row["cnt"] if total_runs_row else 0

            return {
                "last_run": last_run,
                "avg_duration_last_7": avg_duration,
                "active_sources_count": active_sources,
                "total_runs_count": total_runs,
            }

    async def get_latest_digest_run(self) -> Optional[DigestRun]:
        """Fetch the most recent digest run."""
        query = "SELECT * FROM digest_runs ORDER BY id DESC LIMIT 1"
        async with get_db(self.db_path) as db:
            async with db.execute(query) as cursor:
                row = await cursor.fetchone()
                return DigestRun(**dict(row)) if row else None

    async def purge_old_digest_posts(self, retention_days: int = 14) -> int:
        """Deletes posts older than retention_days."""
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
        query = "DELETE FROM digest_posts WHERE (posted_at IS NOT NULL AND posted_at < ?) OR (posted_at IS NULL AND fetched_at < ?)"
        async with get_db(self.db_path) as db:
            cursor = await db.execute(query, (cutoff, cutoff))
            await db.commit()
            return cursor.rowcount

    async def get_digest_posts(
        self,
        run_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[DigestPost]:
        """Fetch digest posts with optional run_id and status filters."""
        conditions = []
        params: List[Any] = []
        if run_id is not None:
            conditions.append("digest_run_id = ?")
            params.append(run_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)

        query = "SELECT * FROM digest_posts"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)

        async with get_db(self.db_path) as db:
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
                posts = []
                for r in rows:
                    posts.append(DigestPost(**dict(r)))
                return posts

    async def update_digest_post_filter_status(
        self,
        post_id: int,
        status: str,
        discard_reason: Optional[str] = None,
        spam_score: float = 0.0,
        spam_reasons: Optional[str] = None,
        normalized_hash: Optional[str] = None,
        original_source: Optional[str] = None,
    ) -> bool:
        """Updates filter status, discard reasons, spam score, and merged source links for a post."""
        query = """
        UPDATE digest_posts 
        SET status = ?, discard_reason = ?, spam_score = ?, spam_reasons = ?, 
            normalized_hash = COALESCE(?, normalized_hash), 
            original_source = COALESCE(?, original_source)
        WHERE id = ?
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                query,
                (status, discard_reason, spam_score, spam_reasons, normalized_hash, original_source, post_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def bulk_update_digest_posts_filter_status(self, posts: List[DigestPost]) -> int:
        """Batch updates filter statuses for a collection of posts."""
        if not posts:
            return 0
        query = """
        UPDATE digest_posts 
        SET status = ?, discard_reason = ?, spam_score = ?, spam_reasons = ?, 
            normalized_hash = ?, original_source = ?
        WHERE id = ?
        """
        params = [
            (
                p.status,
                p.discard_reason,
                p.spam_score,
                p.spam_reasons,
                p.normalized_hash,
                p.original_source,
                p.id,
            )
            for p in posts
            if p.id is not None
        ]
        updated = 0
        async with get_db(self.db_path) as db:
            for param in params:
                cur = await db.execute(query, param)
                if cur.rowcount > 0:
                    updated += 1
            await db.commit()
        return updated

    async def count_filtered_spam_posts(self, run_id: Optional[int] = None) -> int:
        """Counts filtered spam/ad and duplicate posts, optionally for a specific run."""
        query = "SELECT COUNT(*) as cnt FROM digest_posts WHERE status = 'filtered'"
        params: List[Any] = []
        if run_id is not None:
            query += " AND digest_run_id = ?"
            params.append(run_id)

        async with get_db(self.db_path) as db:
            async with db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
                return row["cnt"] if row else 0

    async def update_digest_post_classification(
        self,
        post_id: int,
        topic: Optional[str],
        importance: int,
        status: str,
        discard_reason: Optional[str] = None,
        extracted_json: Optional[str] = None,
    ) -> bool:
        """Updates classification topic, importance, status, discard reason, and extracted JSON."""
        query = """
        UPDATE digest_posts 
        SET topic = ?, importance = ?, status = ?, discard_reason = ?, extracted_json = ?
        WHERE id = ?
        """
        async with get_db(self.db_path) as db:
            cursor = await db.execute(
                query,
                (topic, importance, status, discard_reason, extracted_json, post_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def bulk_update_digest_posts_classification(self, posts: List[DigestPost]) -> int:
        """Batch updates classification fields for a collection of posts."""
        if not posts:
            return 0
        query = """
        UPDATE digest_posts 
        SET topic = ?, importance = ?, status = ?, discard_reason = ?, extracted_json = ?
        WHERE id = ?
        """
        params = [
            (
                p.topic,
                p.importance,
                p.status,
                p.discard_reason,
                p.extracted_json,
                p.id,
            )
            for p in posts
            if p.id is not None
        ]
        updated = 0
        async with get_db(self.db_path) as db:
            for param in params:
                cur = await db.execute(query, param)
                if cur.rowcount > 0:
                    updated += 1
            await db.commit()
        return updated

    async def get_classified_posts_summary(self, run_id: Optional[int] = None) -> Dict[str, int]:
        """Returns topic count breakdown for classified posts."""
        query = "SELECT topic, COUNT(*) as cnt FROM digest_posts WHERE status = 'classified'"
        params: List[Any] = []
        if run_id is not None:
            query += " AND digest_run_id = ?"
            params.append(run_id)
        query += " GROUP BY topic"

        summary: Dict[str, int] = {}
        async with get_db(self.db_path) as db:
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    if r["topic"]:
                        summary[r["topic"]] = r["cnt"]
        return summary

    async def has_scheduled_digest_run_today(self, date_str: str) -> bool:
        """
        Checks if a scheduled digest run was already completed for date_str (YYYY-MM-DD).
        """
        query = """
        SELECT COUNT(*) as cnt FROM digest_runs 
        WHERE trigger = 'scheduled' AND status = 'completed' 
          AND date(started_at) = date(?)
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (date_str,)) as cursor:
                row = await cursor.fetchone()
                return (row["cnt"] > 0) if row else False

    async def get_excluded_digest_posts(
        self,
        run_id: Optional[int] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> Tuple[List[DigestPost], int]:
        """
        Fetches excluded/discarded/filtered posts with pagination and total count.
        """
        where_clauses = ["status IN ('filtered', 'discarded')"]
        params: List[Any] = []
        if run_id is not None:
            where_clauses.append("digest_run_id = ?")
            params.append(run_id)

        where_str = " WHERE " + " AND ".join(where_clauses)
        count_query = f"SELECT COUNT(*) as cnt FROM digest_posts {where_str}"
        select_query = f"SELECT * FROM digest_posts {where_str} ORDER BY id DESC LIMIT ? OFFSET ?"

        async with get_db(self.db_path) as db:
            async with db.execute(count_query, tuple(params)) as cur:
                count_row = await cur.fetchone()
                total = count_row["cnt"] if count_row else 0

            fetch_params = list(params) + [limit, offset]
            async with db.execute(select_query, tuple(fetch_params)) as cur:
                rows = await cur.fetchall()
                posts = [DigestPost(**dict(r)) for r in rows]

            return (posts, total)

    # ------------------ CONTACT MEMORY (V7) ------------------
    async def bind_mem_contact(
        self,
        contact_user_id: int,
        username: Optional[str] = None,
    ) -> MemContact:
        """
        Binds or verifies contact record in mem_contacts.
        Keyed strictly by numeric contact_user_id (H2).
        """
        if username and username.startswith("@"):
            username = username[1:]

        query = """
        INSERT INTO mem_contacts (contact_user_id, username_at_bind, memory_enabled, bound_at)
        VALUES (?, ?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(contact_user_id) DO UPDATE SET
            username_at_bind = COALESCE(excluded.username_at_bind, mem_contacts.username_at_bind);
        """
        async with self.serialized_transaction() as db:
            await db.execute(query, (contact_user_id, username))

        contact = await self.get_mem_contact(contact_user_id)
        if not contact:
            raise RuntimeError(f"Failed to retrieve bound mem_contact for {contact_user_id}")
        return contact

    async def get_mem_contact(self, contact_user_id: int) -> Optional[MemContact]:
        """Fetch mem_contact by numeric contact_user_id."""
        query = "SELECT * FROM mem_contacts WHERE contact_user_id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id,)) as cur:
                row = await cur.fetchone()
                return MemContact(**dict(row)) if row else None

    async def get_mem_contact_by_username(self, username: str) -> Optional[MemContact]:
        """Fetch mem_contact by username."""
        clean_name = username.lstrip("@").strip()
        query = "SELECT * FROM mem_contacts WHERE LOWER(username_at_bind) = LOWER(?)"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (clean_name,)) as cur:
                row = await cur.fetchone()
                return MemContact(**dict(row)) if row else None

    async def pause_mem_contact(self, contact_user_id: int, reason: str) -> bool:
        """Pauses memory recording for contact with reason."""
        query = "UPDATE mem_contacts SET memory_enabled = 0, paused_reason = ? WHERE contact_user_id = ?"
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (reason, contact_user_id))
            return cur.rowcount > 0

    async def resume_mem_contact(self, contact_user_id: int) -> bool:
        """Resumes memory recording for contact."""
        query = "UPDATE mem_contacts SET memory_enabled = 1, paused_reason = NULL WHERE contact_user_id = ?"
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (contact_user_id,))
            return cur.rowcount > 0

    async def log_mem_message(
        self,
        contact_user_id: int,
        tg_message_id: int,
        sender: str,
        text: str,
        is_media: bool = False,
        sent_at: Optional[str] = None,
    ) -> Optional[int]:
        """
        Logs message turn into mem_messages with pure code redaction before storage (H5).
        Sender must be one of: 'contact', 'owner_manual', 'owner_edited', 'bot_approved', 'bot_auto' (H8).
        Enforces UNIQUE(contact_user_id, tg_message_id).
        """
        allowed_senders = ("contact", "owner_manual", "owner_edited", "bot_approved", "bot_auto")
        if sender not in allowed_senders:
            raise ValueError(f"Invalid sender '{sender}'. Must be one of {allowed_senders}")

        # Strict code-level redaction BEFORE touching DB (H5)
        text_redacted = redact_text(text)

        query = """
        INSERT OR IGNORE INTO mem_messages (
            contact_user_id, tg_message_id, sender, text_redacted, is_media, sent_at
        ) VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
        """
        async with self.serialized_transaction() as db:
            cur = await db.execute(
                query,
                (
                    contact_user_id,
                    tg_message_id,
                    sender,
                    text_redacted,
                    1 if is_media else 0,
                    sent_at,
                ),
            )
            return cur.lastrowid if cur.rowcount > 0 else None

    async def update_mem_message_edit(
        self,
        contact_user_id: int,
        tg_message_id: int,
        new_text: str,
    ) -> bool:
        """
        Updates text_redacted and edited_at for an existing message turn.
        Re-applies full redaction before persistence.
        """
        text_redacted = redact_text(new_text)
        query = """
        UPDATE mem_messages 
        SET text_redacted = ?, edited_at = CURRENT_TIMESTAMP
        WHERE contact_user_id = ? AND tg_message_id = ?
        """
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (text_redacted, contact_user_id, tg_message_id))
            return cur.rowcount > 0

    async def get_mem_message(
        self,
        contact_user_id: int,
        tg_message_id: int,
    ) -> Optional[MemMessage]:
        """Fetch single mem_message by contact_user_id and tg_message_id."""
        query = "SELECT * FROM mem_messages WHERE contact_user_id = ? AND tg_message_id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id, tg_message_id)) as cur:
                row = await cur.fetchone()
                return MemMessage(**dict(row)) if row else None

    async def get_mem_message_by_id(
        self,
        contact_user_id: int,
        message_id: int,
    ) -> Optional[MemMessage]:
        """Fetch single mem_message by contact_user_id and DB primary key id (H4)."""
        query = "SELECT * FROM mem_messages WHERE contact_user_id = ? AND id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id, message_id)) as cur:
                row = await cur.fetchone()
                if row:
                    msg = MemMessage(**dict(row))
                    assert msg.contact_user_id == contact_user_id, "CRITICAL: Message isolation breach!"
                    return msg
                return None

    async def get_mem_messages(
        self,
        contact_user_id: int,
        limit: int = 50,
    ) -> List[MemMessage]:
        """
        Fetch recent messages for target contact.
        Enforces strict per-contact isolation (H4).
        """
        query = """
        SELECT * FROM mem_messages 
        WHERE contact_user_id = ? 
        ORDER BY id ASC 
        LIMIT ?
        """
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id, limit)) as cur:
                rows = await cur.fetchall()
                # Strict code-level isolation assertion (H4)
                messages = []
                for r in rows:
                    msg = MemMessage(**dict(r))
                    assert msg.contact_user_id == contact_user_id, "CRITICAL: Memory isolation breach!"
                    messages.append(msg)
                return messages

    async def purge_old_mem_messages(self, retention_days: int = 180) -> int:
        """
        Purges messages older than retention_days from mem_messages.
        """
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
        query = "DELETE FROM mem_messages WHERE sent_at < ?"
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (cutoff,))
            return cur.rowcount

    async def get_mem_state(self, key: str) -> Optional[str]:
        """Fetch persistent value from mem_state."""
        query = "SELECT value FROM mem_state WHERE key = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (key,)) as cur:
                row = await cur.fetchone()
                return row["value"] if row else None

    async def set_mem_state(self, key: str, value: str) -> None:
        """Upsert persistent key-value into mem_state."""
        query = """
        INSERT INTO mem_state (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP;
        """
        async with self.serialized_transaction() as db:
            await db.execute(query, (key, value))

    async def create_mem_fact(
        self,
        contact_user_id: int,
        category: Optional[str],
        text: str,
        source_type: str,
        source_message_ids: Optional[List[int]] = None,
        confidence: float = 1.0,
        supersedes_id: Optional[int] = None,
        valid_until: Optional[str] = None,
        injection_suspected: bool = False,
    ) -> int:
        """Stores extracted fact with strict source_type validation and redaction."""
        src_json = json.dumps(source_message_ids or [])
        text_redacted = redact_text(text)
        category = normalize_category(category)
        query = """
        INSERT INTO mem_facts (
            contact_user_id, category, text, source_type, source_message_ids,
            confidence, supersedes_id, valid_until, injection_suspected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        async with self.serialized_transaction() as db:
            cur = await db.execute(
                query,
                (
                    contact_user_id,
                    category,
                    text_redacted,
                    source_type,
                    src_json,
                    confidence,
                    supersedes_id,
                    valid_until,
                    1 if injection_suspected else 0,
                ),
            )
            return cur.lastrowid

    async def get_mem_facts(
        self,
        contact_user_id: int,
        status: str = "active",
    ) -> List[MemFact]:
        """Fetch active facts for target contact with strict isolation (H4)."""
        query = "SELECT * FROM mem_facts WHERE contact_user_id = ? AND status = ? ORDER BY id ASC"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id, status)) as cur:
                rows = await cur.fetchall()
                facts = []
                for r in rows:
                    fact = MemFact(**dict(r))
                    assert fact.contact_user_id == contact_user_id, "CRITICAL: Fact isolation breach!"
                    facts.append(fact)
                return facts

    async def get_mem_fact_by_id(
        self,
        contact_user_id: int,
        fact_id: int,
    ) -> Optional[MemFact]:
        """Fetch single mem_fact by contact_user_id and DB primary key id (H4)."""
        query = "SELECT * FROM mem_facts WHERE contact_user_id = ? AND id = ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id, fact_id)) as cur:
                row = await cur.fetchone()
                if row:
                    fact = MemFact(**dict(row))
                    assert fact.contact_user_id == contact_user_id, "CRITICAL: Fact isolation breach!"
                    return fact
                return None

    async def create_mem_open_item(
        self,
        contact_user_id: int,
        text: str,
        source_message_ids: Optional[List[int]] = None,
    ) -> int:
        """Stores open item for target contact."""
        src_json = json.dumps(source_message_ids or [])
        text_redacted = redact_text(text)
        query = """
        INSERT INTO mem_open_items (contact_user_id, text, source_message_ids)
        VALUES (?, ?, ?)
        """
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (contact_user_id, text_redacted, src_json))
            return cur.lastrowid

    async def get_mem_open_items(
        self,
        contact_user_id: int,
        status: str = "open",
    ) -> List[MemOpenItem]:
        """Fetch open items for target contact with strict isolation (H4)."""
        query = "SELECT * FROM mem_open_items WHERE contact_user_id = ? AND status = ? ORDER BY id ASC"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id, status)) as cur:
                rows = await cur.fetchall()
                items = []
                for r in rows:
                    item = MemOpenItem(**dict(r))
                    assert item.contact_user_id == contact_user_id, "CRITICAL: Open item isolation breach!"
                    items.append(item)
                return items

    async def supersede_mem_fact(
        self,
        contact_user_id: int,
        old_fact_id: int,
        new_fact_id: int,
    ) -> bool:
        """Marks old_fact_id as superseded by new_fact_id (H4)."""
        query_old = "UPDATE mem_facts SET status = 'superseded', updated_at = CURRENT_TIMESTAMP WHERE contact_user_id = ? AND id = ?"
        query_new = "UPDATE mem_facts SET supersedes_id = ?, updated_at = CURRENT_TIMESTAMP WHERE contact_user_id = ? AND id = ?"
        async with self.serialized_transaction() as db:
            cur_old = await db.execute(query_old, (contact_user_id, old_fact_id))
            cur_new = await db.execute(query_new, (old_fact_id, contact_user_id, new_fact_id))
            return cur_old.rowcount > 0 and cur_new.rowcount > 0

    async def update_mem_fact_status(
        self,
        contact_user_id: int,
        fact_id: int,
        status: str,
    ) -> bool:
        """Updates status of a fact ('active', 'superseded', 'disputed')."""
        if status not in ("active", "superseded", "disputed"):
            raise ValueError(f"Invalid status '{status}'. Must be active, superseded, or disputed.")
        query = "UPDATE mem_facts SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE contact_user_id = ? AND id = ?"
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (status, contact_user_id, fact_id))
            return cur.rowcount > 0

    async def resolve_mem_open_item(
        self,
        contact_user_id: int,
        item_id: int,
        status: str = "resolved",
    ) -> bool:
        """Resolves or dismisses an open item."""
        if status not in ("open", "resolved", "dismissed"):
            raise ValueError(f"Invalid status '{status}'. Must be open, resolved, or dismissed.")
        query = "UPDATE mem_open_items SET status = ? WHERE contact_user_id = ? AND id = ?"
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (status, contact_user_id, item_id))
            return cur.rowcount > 0

    async def save_mem_summary(
        self,
        contact_user_id: int,
        summary_text: str,
        covers_until_message_id: Optional[int],
        version: int = 1,
    ) -> int:
        """Saves a rolling summary into mem_summaries with pure redaction (H5)."""
        clean_summary = redact_text(summary_text)
        query = """
        INSERT INTO mem_summaries (contact_user_id, summary_text, covers_until_message_id, version, created_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        """
        async with self.serialized_transaction() as db:
            cur = await db.execute(
                query,
                (contact_user_id, clean_summary, covers_until_message_id, version),
            )
            return cur.lastrowid

    async def get_latest_mem_summary(
        self,
        contact_user_id: int,
    ) -> Optional[MemSummary]:
        """Fetches the latest summary for target contact with strict isolation (H4)."""
        query = "SELECT * FROM mem_summaries WHERE contact_user_id = ? ORDER BY id DESC LIMIT 1"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    summary = MemSummary(**dict(row))
                    assert summary.contact_user_id == contact_user_id, "CRITICAL: Summary isolation breach!"
                    return summary
                return None

    async def get_mem_summaries_history(
        self,
        contact_user_id: int,
        limit: int = 5,
    ) -> List[MemSummary]:
        """Fetches historical summaries for target contact."""
        query = "SELECT * FROM mem_summaries WHERE contact_user_id = ? ORDER BY id DESC LIMIT ?"
        async with get_db(self.db_path) as db:
            async with db.execute(query, (contact_user_id, limit)) as cur:
                rows = await cur.fetchall()
                results = []
                for r in rows:
                    summary = MemSummary(**dict(r))
                    assert summary.contact_user_id == contact_user_id, "CRITICAL: Summary isolation breach!"
                    results.append(summary)
                return results

    async def get_mem_messages_since(
        self,
        contact_user_id: int,
        since_message_id: Optional[int] = None,
        limit: int = 50,
    ) -> List[MemMessage]:
        """Fetches messages for contact created after since_message_id (H4)."""
        if since_message_id is not None:
            query = """
            SELECT * FROM mem_messages 
            WHERE contact_user_id = ? AND id > ? 
            ORDER BY id ASC 
            LIMIT ?
            """
            params: Tuple[Any, ...] = (contact_user_id, since_message_id, limit)
        else:
            query = """
            SELECT * FROM mem_messages 
            WHERE contact_user_id = ? 
            ORDER BY id ASC 
            LIMIT ?
            """
            params = (contact_user_id, limit)

        async with get_db(self.db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
                results = []
                for r in rows:
                    m = MemMessage(**dict(r))
                    assert m.contact_user_id == contact_user_id, "CRITICAL: Message isolation breach!"
                    results.append(m)
                return results

    # ------------------ RETRIEVAL & EMBEDDING CRUD (PHASE 2) ------------------
    async def save_mem_embedding(
        self,
        contact_user_id: int,
        item_type: str,
        item_id: int,
        model_name: str,
        dim: int,
        vector: bytes,
    ) -> int:
        """
        Saves or updates embedding vector BLOB in mem_embeddings.
        item_type must be in ('fact', 'message', 'summary').
        """
        if item_type not in ("fact", "message", "summary"):
            raise ValueError(f"Invalid item_type '{item_type}'. Must be fact, message, or summary.")

        delete_query = "DELETE FROM mem_embeddings WHERE item_type = ? AND item_id = ?"
        insert_query = """
        INSERT INTO mem_embeddings (contact_user_id, item_type, item_id, model_name, dim, vector)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        async with self.serialized_transaction() as db:
            await db.execute(delete_query, (item_type, item_id))
            cur = await db.execute(
                insert_query,
                (contact_user_id, item_type, item_id, model_name, dim, vector),
            )
            return cur.lastrowid

    async def get_mem_embeddings_by_contact(
        self,
        contact_user_id: int,
        item_type: Optional[str] = None,
    ) -> List[MemEmbedding]:
        """
        Fetches all stored embeddings for target contact.
        Enforces strict SQL-level and code-level isolation (H4).
        """
        if item_type:
            query = "SELECT * FROM mem_embeddings WHERE contact_user_id = ? AND item_type = ?"
            params: Tuple[Any, ...] = (contact_user_id, item_type)
        else:
            query = "SELECT * FROM mem_embeddings WHERE contact_user_id = ?"
            params = (contact_user_id,)

        async with get_db(self.db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
                results = []
                for r in rows:
                    emb = MemEmbedding(**dict(r))
                    assert emb.contact_user_id == contact_user_id, "CRITICAL: Cross-contact embedding leak detected!"
                    results.append(emb)
                return results

    async def search_mem_messages_fts(
        self,
        contact_user_id: int,
        query_text: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Searches mem_messages using SQLite FTS5 BM25 ranking.
        Enforces strict per-contact SQL filtering (H4) and query sanitization.
        """
        if not query_text or not query_text.strip():
            return []

        tokens = re.findall(r"\w+", query_text)
        if not tokens:
            return []

        sanitized_query = " OR ".join(f'"{tok}"' for tok in tokens if len(tok) > 1)
        if not sanitized_query:
            sanitized_query = " OR ".join(f'"{tok}"' for tok in tokens)

        query = """
        SELECT m.id, m.contact_user_id, m.tg_message_id, m.sender, m.text_redacted,
               m.sent_at, m.edited_at, m.is_media, bm25(mem_messages_fts) as rank
        FROM mem_messages m
        JOIN mem_messages_fts ON m.id = mem_messages_fts.rowid
        WHERE m.contact_user_id = ? AND mem_messages_fts MATCH ?
        ORDER BY rank ASC
        LIMIT ?
        """
        async with get_db(self.db_path) as db:
            try:
                async with db.execute(query, (contact_user_id, sanitized_query, limit)) as cur:
                    rows = await cur.fetchall()
                    results = []
                    for r in rows:
                        d = dict(r)
                        assert d["contact_user_id"] == contact_user_id, "CRITICAL: Cross-contact FTS message leak!"
                        results.append(d)
                    return results
            except Exception:
                return []

    async def search_mem_facts_fts(
        self,
        contact_user_id: int,
        query_text: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Searches active mem_facts using SQLite FTS5 BM25 ranking.
        Enforces strict per-contact SQL filtering (H4) and query sanitization.
        """
        if not query_text or not query_text.strip():
            return []

        tokens = re.findall(r"\w+", query_text)
        if not tokens:
            return []

        sanitized_query = " OR ".join(f'"{tok}"' for tok in tokens if len(tok) > 1)
        if not sanitized_query:
            sanitized_query = " OR ".join(f'"{tok}"' for tok in tokens)

        query = """
        SELECT f.id, f.contact_user_id, f.category, f.text, f.source_type,
               f.source_message_ids, f.status, f.confidence, f.supersedes_id,
               f.created_at, f.valid_until, bm25(mem_facts_fts) as rank
        FROM mem_facts f
        JOIN mem_facts_fts ON f.id = mem_facts_fts.rowid
        WHERE f.contact_user_id = ? AND f.status = 'active' AND mem_facts_fts MATCH ?
        ORDER BY rank ASC
        LIMIT ?
        """
        async with get_db(self.db_path) as db:
            try:
                async with db.execute(query, (contact_user_id, sanitized_query, limit)) as cur:
                    rows = await cur.fetchall()
                    results = []
                    for r in rows:
                        d = dict(r)
                        assert d["contact_user_id"] == contact_user_id, "CRITICAL: Cross-contact FTS fact leak!"
                        results.append(d)
                    return results
            except Exception:
                return []

    async def delete_mem_embeddings_by_contact(self, contact_user_id: int) -> int:
        """Deletes all embeddings for contact."""
        query = "DELETE FROM mem_embeddings WHERE contact_user_id = ?"
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (contact_user_id,))
            return cur.rowcount

    async def get_unembedded_messages(
        self,
        contact_user_id: Optional[int] = None,
        limit: int = 20,
    ) -> List[MemMessage]:
        """Fetches messages that have not yet been embedded."""
        if contact_user_id is not None:
            query = """
            SELECT m.* FROM mem_messages m
            LEFT JOIN mem_embeddings e ON e.item_type = 'message' AND e.item_id = m.id
            WHERE e.id IS NULL AND m.contact_user_id = ?
            ORDER BY m.id DESC LIMIT ?
            """
            params: Tuple[Any, ...] = (contact_user_id, limit)
        else:
            query = """
            SELECT m.* FROM mem_messages m
            LEFT JOIN mem_embeddings e ON e.item_type = 'message' AND e.item_id = m.id
            WHERE e.id IS NULL
            ORDER BY m.id DESC LIMIT ?
            """
            params = (limit,)

        async with get_db(self.db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
                return [MemMessage(**dict(r)) for r in rows]

    async def get_unembedded_facts(
        self,
        contact_user_id: Optional[int] = None,
        limit: int = 20,
    ) -> List[MemFact]:
        """Fetches active facts that have not yet been embedded."""
        if contact_user_id is not None:
            query = """
            SELECT f.* FROM mem_facts f
            LEFT JOIN mem_embeddings e ON e.item_type = 'fact' AND e.item_id = f.id
            WHERE e.id IS NULL AND f.status = 'active' AND f.contact_user_id = ?
            ORDER BY f.id DESC LIMIT ?
            """
            params: Tuple[Any, ...] = (contact_user_id, limit)
        else:
            query = """
            SELECT f.* FROM mem_facts f
            LEFT JOIN mem_embeddings e ON e.item_type = 'fact' AND e.item_id = f.id
            WHERE e.id IS NULL AND f.status = 'active'
            ORDER BY f.id DESC LIMIT ?
            """
            params = (limit,)

        async with get_db(self.db_path) as db:
            async with db.execute(query, params) as cur:
                rows = await cur.fetchall()
                return [MemFact(**dict(r)) for r in rows]

    async def get_daily_counter(self, prefix: str) -> int:
        """Fetches daily usage counter from mem_state."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        key = f"daily_count:{prefix}:{today}"
        val = await self.get_mem_state(key)
        return int(val) if val and val.isdigit() else 0

    async def increment_daily_counter(self, prefix: str) -> int:
        """Increments and returns daily usage counter in mem_state."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        key = f"daily_count:{prefix}:{today}"
        current = await self.get_daily_counter(prefix)
        new_val = current + 1
        await self.set_mem_state(key, str(new_val))
        return new_val

    # ------------------ OWNER MEMORY MANAGEMENT (PHASE 6) ------------------
    async def wipe_contact_memory(self, contact_user_id: int) -> Dict[str, int]:
        """
        Permanently deletes all memory for target contact per Rule H10:
        - mem_messages (auto-triggers clean mem_messages_fts)
        - mem_facts (auto-triggers clean mem_facts_fts)
        - mem_open_items
        - mem_summaries
        - mem_embeddings
        - mem_contacts
        - blanks drafts table incoming_text and proposed_reply
        Returns dictionary of affected row counts.
        """
        counts: Dict[str, int] = {}
        async with self.serialized_transaction() as db:
            cur = await db.execute("DELETE FROM mem_messages WHERE contact_user_id = ?", (contact_user_id,))
            counts["messages"] = cur.rowcount
            cur = await db.execute("DELETE FROM mem_facts WHERE contact_user_id = ?", (contact_user_id,))
            counts["facts"] = cur.rowcount
            cur = await db.execute("DELETE FROM mem_open_items WHERE contact_user_id = ?", (contact_user_id,))
            counts["open_items"] = cur.rowcount
            cur = await db.execute("DELETE FROM mem_summaries WHERE contact_user_id = ?", (contact_user_id,))
            counts["summaries"] = cur.rowcount
            cur = await db.execute("DELETE FROM mem_embeddings WHERE contact_user_id = ?", (contact_user_id,))
            counts["embeddings"] = cur.rowcount
            cur = await db.execute("DELETE FROM mem_contacts WHERE contact_user_id = ?", (contact_user_id,))
            counts["contact_records"] = cur.rowcount
            cur = await db.execute(
                "UPDATE drafts SET incoming_text = '[DELETED]', proposed_reply = '[DELETED]' WHERE contact_user_id = ?",
                (contact_user_id,),
            )
            counts["drafts_blanked"] = cur.rowcount
            # Step 1.7: Clear conversation_history for this contact
            cur = await db.execute("DELETE FROM conversation_history WHERE chat_id = ?", (contact_user_id,))
            counts["conversation_history"] = cur.rowcount
            # Clear scheduled_tasks for this contact
            cur = await db.execute(
                "DELETE FROM scheduled_tasks WHERE target_peer = ? OR target_peer LIKE ?",
                (str(contact_user_id), f"%{contact_user_id}%"),
            )
            counts["scheduled_tasks"] = cur.rowcount
        return counts

    async def confirm_mem_fact(
        self,
        contact_user_id: int,
        fact_id: int,
    ) -> bool:
        """Sets source_type='owner_confirmed' and confidence=1.0 for an active fact per Step 1.2."""
        query = """
        UPDATE mem_facts
        SET source_type = 'owner_confirmed', confidence = 1.0, updated_at = CURRENT_TIMESTAMP
        WHERE contact_user_id = ? AND id = ?
        """
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (contact_user_id, fact_id))
            return cur.rowcount > 0

    async def cleanup_conversation_history(self, days: int = 180) -> int:
        """Purge conversation_history rows older than retention threshold per Step 1.7."""
        query = "DELETE FROM conversation_history WHERE timestamp < datetime('now', ?)"
        async with self.serialized_transaction() as db:
            cur = await db.execute(query, (f"-{days} days",))
            return cur.rowcount

    async def delete_mem_fact(self, contact_user_id: int, fact_id: int) -> bool:
        """Permanently deletes a specific fact for target contact (H4, H10)."""
        async with self.serialized_transaction() as db:
            cur = await db.execute(
                "DELETE FROM mem_facts WHERE contact_user_id = ? AND id = ?",
                (contact_user_id, fact_id),
            )
            return cur.rowcount > 0

    async def get_mem_stats(self, contact_user_id: int) -> Dict[str, int]:
        """Returns statistics of stored memory items for target contact (H4)."""
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM mem_messages WHERE contact_user_id = ?", (contact_user_id,)) as cur:
                messages = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_facts WHERE contact_user_id = ? AND status = 'active'", (contact_user_id,)) as cur:
                facts = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_open_items WHERE contact_user_id = ? AND status = 'open'", (contact_user_id,)) as cur:
                open_items = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_summaries WHERE contact_user_id = ?", (contact_user_id,)) as cur:
                summaries = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_embeddings WHERE contact_user_id = ?", (contact_user_id,)) as cur:
                embeddings = (await cur.fetchone())[0]
            return {
                "messages": messages,
                "facts": facts,
                "open_items": open_items,
                "summaries": summaries,
                "embeddings": embeddings,
            }

    async def get_overall_mem_stats(self) -> Dict[str, int]:
        """Returns overall memory statistics across all contacts."""
        async with get_db(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM mem_contacts WHERE memory_enabled = 1") as cur:
                contacts_enabled = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_messages") as cur:
                messages = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_facts WHERE status = 'active'") as cur:
                facts = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_open_items WHERE status = 'open'") as cur:
                open_items = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_summaries") as cur:
                summaries = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM mem_embeddings") as cur:
                embeddings = (await cur.fetchone())[0]
            return {
                "contacts_enabled": contacts_enabled,
                "messages": messages,
                "facts": facts,
                "open_items": open_items,
                "summaries": summaries,
                "embeddings": embeddings,
            }



