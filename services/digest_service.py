"""
Channel and Group Intelligence Digest Service.
Monitors feeds, triggers instant alerts on critical keywords, and provides scheduled summaries.
"""

import logging
from typing import List, Callable, Optional
from config import settings
from database.crud import DatabaseManager
from services.ai_engine import AIEngine

logger = logging.getLogger(__name__)


class DigestService:
    def __init__(self, db_manager: DatabaseManager, ai_engine: AIEngine):
        self.db_manager = db_manager
        self.ai_engine = ai_engine
        self._notify_boss_callback: Optional[Callable] = None
        self.alert_keywords = ["urgent", "muhim", "diqqat", "shoshilinch", "xavf", "vazifa"]

    def register_boss_notifier(self, callback: Callable):
        self._notify_boss_callback = callback

    def set_custom_alert_keywords(self, keywords: List[str]):
        self.alert_keywords = [k.strip().lower() for k in keywords]

    async def process_incoming_post(
        self,
        peer_id: int,
        source_title: str,
        message_id: int,
        text: str,
        is_group: bool = False,
    ):
        """Processes incoming feed/group post."""
        if not text:
            return

        # 1. Save to raw feed
        await self.db_manager.save_feed_post(
            source_peer_id=peer_id,
            source_title=source_title,
            message_id=message_id,
            post_text=text,
        )

        # 2. Check for instant critical alerts
        lower_text = text.lower()
        matched = [kw for kw in self.alert_keywords if kw in lower_text]
        if matched and self._notify_boss_callback:
            import html
            source_type = "Guruh" if is_group else "Kanal"
            safe_title = html.escape(source_title)
            safe_keywords = html.escape(", ".join(matched))
            safe_post = html.escape(text[:400])
            alert_msg = (
                f"🚨 <b>TEZKOR OGOHLANTIRISH!</b>\n\n"
                f"📢 <b>{source_type}:</b> {safe_title}\n"
                f"🔍 <b>Topilgan kalit so'zlar:</b> <code>{safe_keywords}</code>\n"
                f"📝 <b>Post mazmuni:</b>\n<i>{safe_post}</i>"
            )
            await self._notify_boss_callback(alert_msg)

    async def generate_digest(self, force_recent: bool = False) -> tuple[str, int]:
        """
        Collects unsummarized or recent feed posts and returns AI summary and post count.
        """
        posts = await self.db_manager.get_unsummarized_posts(limit=30)
        unsum_ids = [p["id"] for p in posts]

        if not posts or force_recent:
            posts = await self.db_manager.get_recent_posts(limit=25)

        if not posts:
            return ("", 0)

        summary = await self.ai_engine.summarize_feed(posts)

        if unsum_ids and not force_recent:
            await self.db_manager.mark_posts_summarized(unsum_ids)

        return (summary, len(posts))

    async def generate_and_send_digest(self) -> str:
        """Collects posts, generates executive briefing and notifies boss if callback is registered."""
        summary, count = await self.generate_digest()
        if not summary:
            return "Hozircha yangi tahlil qilinadigan xabarlar yo'q."

        if self._notify_boss_callback:
            await self._notify_boss_callback(summary)

        return summary

    async def get_stats(self) -> dict:
        """Retrieves monitoring statistics."""
        return await self.db_manager.get_feed_statistics()
