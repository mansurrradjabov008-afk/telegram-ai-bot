"""
Asynchronous Database Manager and CRUD operations for Telegram AI Butler.
"""

from typing import List, Optional
from datetime import datetime
import aiosqlite
from .connection import get_db
from .models import Contact, ScheduledTask, ConversationMessage, MonitoredSource


class DatabaseManager:
    def __init__(self, db_path=None):
        self.db_path = db_path

    # ------------------ CONTACT CRM ------------------
    async def upsert_contact(
        self,
        user_id: int,
        full_name: str,
        username: Optional[str] = None,
        relationship_type: str = "general",
        addressing_style: str = "siz",
        custom_instructions: Optional[str] = None,
        language_code: str = "uz",
    ) -> Contact:
        """Add or update contact relationship metadata."""
        if username and username.startswith("@"):
            username = username[1:]

        query = """
        INSERT INTO contacts (user_id, username, full_name, relationship_type, addressing_style, custom_instructions, language_code, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username=COALESCE(excluded.username, contacts.username),
            full_name=excluded.full_name,
            relationship_type=excluded.relationship_type,
            addressing_style=excluded.addressing_style,
            custom_instructions=COALESCE(excluded.custom_instructions, contacts.custom_instructions),
            language_code=excluded.language_code,
            updated_at=CURRENT_TIMESTAMP;
        """
        async with get_db(self.db_path) as db:
            await db.execute(
                query,
                (user_id, username, full_name, relationship_type, addressing_style, custom_instructions, language_code),
            )
            await db.commit()

        return await self.get_contact_by_id(user_id)

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
        """Save chat turn into history."""
        query = """
        INSERT INTO conversation_history (chat_id, sender_name, role, content)
        VALUES (?, ?, ?, ?)
        """
        async with get_db(self.db_path) as db:
            await db.execute(query, (chat_id, sender_name, role, content))
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
