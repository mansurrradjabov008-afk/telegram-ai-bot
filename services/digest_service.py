"""
Channel and Group Intelligence Digest Service.
Monitors feeds, triggers instant alerts on critical keywords, and provides scheduled summaries.
Includes Smart Digest V2 sources whitelist, post collection, and storage pipelines.
"""

import asyncio
import html
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import List, Callable, Optional, Dict, Any, Tuple

try:
    from telethon.errors import FloodWaitError
except ImportError:
    class FloodWaitError(Exception):
        seconds = 0

from config import settings
from database.crud import DatabaseManager
from database.models import DigestSource, DigestPost, DigestRun
from services.ai_engine import AIEngine
from services.digest_cleaner import DigestCleaner
from services.digest_classifier import DigestClassifier
from services.digest_summarizer import DigestSummarizer, DigestItem
from services.digest_formatter import DigestFormatter

logger = logging.getLogger(__name__)


class DigestService:
    def __init__(self, db_manager: DatabaseManager, ai_engine: AIEngine):
        self.db_manager = db_manager
        self.ai_engine = ai_engine
        self.cleaner = DigestCleaner()
        self.classifier = DigestClassifier(self.ai_engine)
        self.summarizer = DigestSummarizer(self.ai_engine)
        self.formatter = DigestFormatter()
        self._is_run_in_progress: bool = False
        self._notify_boss_callback: Optional[Callable] = None
        self._telethon_client: Optional[Any] = None
        self._client_getter: Optional[Callable] = None
        self.alert_keywords = ["urgent", "muhim", "diqqat", "shoshilinch", "xavf", "vazifa"]

    def register_boss_notifier(self, callback: Callable):
        """Registers callback to notify owner/admin cockpit."""
        self._notify_boss_callback = callback

    def set_userbot_client(self, client_or_getter: Any):
        """Sets Telethon client instance or a callable returning it."""
        import inspect
        if inspect.isfunction(client_or_getter) or inspect.ismethod(client_or_getter):
            self._client_getter = client_or_getter
            self._telethon_client = None
        else:
            self._telethon_client = client_or_getter
            self._client_getter = None

    def get_client(self) -> Optional[Any]:
        """Retrieves active Telethon client."""
        if self._client_getter:
            return self._client_getter()
        return self._telethon_client

    def set_custom_alert_keywords(self, keywords: List[str]):
        """Sets custom keywords for instant alert matching."""
        self.alert_keywords = [k.strip().lower() for k in keywords]

    # ------------------ LEGACY V1 PIPELINE (BACKWARDS COMPATIBILITY) ------------------
    async def process_incoming_post(
        self,
        peer_id: int,
        source_title: str,
        message_id: int,
        text: str,
        is_group: bool = False,
    ):
        """Processes incoming feed/group post for legacy pipeline."""
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

    async def generate_digest(self, force_recent: bool = False) -> Tuple[str, int]:
        """
        Legacy V1 digest generator: collects unsummarized raw_feed_posts and calls AI summary.
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
        """Legacy V1 method: collects posts and notifies boss."""
        summary, count = await self.generate_digest()
        if not summary:
            return "Hozircha yangi tahlil qilinadigan xabarlar yo'q."

        if self._notify_boss_callback:
            await self._notify_boss_callback(summary)

        return summary

    async def get_stats(self) -> dict:
        """Retrieves monitoring statistics."""
        return await self.db_manager.get_feed_statistics()

    # ------------------ SMART DIGEST V2 PIPELINE ------------------
    @staticmethod
    def build_post_link(chat_id: int, message_id: int, username: Optional[str] = None) -> str:
        """
        Builds source link strictly following Anti-Hallucination Rule 2:
        - Public channel/group with username: https://t.me/<username>/<message_id>
        - Private channel/group: https://t.me/c/<internal_id>/<message_id>
          (where internal_id is without the -100 prefix)
        """
        clean_user = username.lstrip("@").strip() if username else ""
        if clean_user:
            return f"https://t.me/{clean_user}/{message_id}"

        # Strip negative sign and 100 prefix for private supergroups/channels
        abs_id = abs(chat_id)
        str_id = str(abs_id)
        if str_id.startswith("100") and len(str_id) > 3:
            internal_id = str_id[3:]
        else:
            internal_id = str_id

        return f"https://t.me/c/{internal_id}/{message_id}"

    async def resolve_and_add_source(
        self,
        client: Any,
        target: str,
    ) -> Tuple[bool, str, Optional[DigestSource]]:
        """
        Resolves entity via Telethon and strictly enforces privacy boundaries:
        - 1-to-1 private chats are REJECTED immediately.
        - Only channels and groups can be added.
        """
        target = target.strip()
        if not target:
            return (False, "⚠️ Manba ko'rsatilmadi. Masalan: <code>/digest add @kanal</code>", None)

        telethon_client = client or self.get_client()
        if not telethon_client:
            return (False, "⚠️ Userbot mijozi ulanmagan.", None)

        # Parse identifier from link or username or numeric id
        identifier: Any = target
        if "t.me/" in target:
            parts = target.split("t.me/")[-1].split("/")
            first = parts[0]
            if first == "c" and len(parts) > 1:
                cid = parts[1]
                identifier = int(f"-100{cid}") if not cid.startswith("-100") else int(cid)
            elif first.startswith("+") or first == "joinchat":
                identifier = target
            else:
                identifier = first.lstrip("@")
        elif target.startswith("@"):
            identifier = target.lstrip("@")
        elif target.lstrip("-").isdigit():
            identifier = int(target)

        try:
            entity = await telethon_client.get_entity(identifier)
        except Exception as e:
            logger.warning(f"Failed to resolve entity {target}: {e}")
            return (False, f"❌ Manba topilmadi yoki unga kirish huquqi yo'q: {str(e)}", None)

        # Strict privacy check: NEVER allow User or private chats
        from telethon.tl.types import User, Channel, Chat

        if isinstance(entity, User) or getattr(entity, "is_private", False):
            return (
                False,
                "❌ <b>Shaxsiy yozishmalarni (1-ga-1 chat) digestga qo'shish qat'iyan man etiladi!</b>\n"
                "Faqat ommaviy yoki yopiq kanal va guruhlarni qo'shishingiz mumkin.",
                None,
            )

        title = getattr(entity, "title", None) or str(target)
        username = getattr(entity, "username", None)

        # Telethon entity ID mapping
        try:
            import telethon.utils
            actual_peer_id = telethon.utils.get_peer_id(entity)
        except Exception:
            actual_peer_id = getattr(entity, "id", None)
            if actual_peer_id and not str(actual_peer_id).startswith("-100") and isinstance(entity, Channel):
                actual_peer_id = int(f"-100{actual_peer_id}")

        peer_type = "channel"
        if isinstance(entity, Chat):
            peer_type = "group"
        elif isinstance(entity, Channel):
            if getattr(entity, "megagroup", False):
                peer_type = "group"
            else:
                peer_type = "channel"

        source = await self.db_manager.add_digest_source(
            peer_id=actual_peer_id,
            title=title,
            peer_type=peer_type,
            username=username,
        )

        type_label = "Kanal" if peer_type == "channel" else "Guruh"
        return (
            True,
            f"✅ <b>{type_label} muvaffaqiyatli qo'shildi!</b>\n\n"
            f"📢 <b>Nomi:</b> {html.escape(title)}\n"
            f"🆔 <b>ID:</b> <code>{actual_peer_id}</code>\n"
            f"🔗 <b>Username:</b> <code>@{username or 'mavjud_emas'}</code>\n"
            f"🏷 <b>Turi:</b> {type_label}",
            source,
        )

    async def remove_source(self, identifier: Any) -> Tuple[bool, str]:
        """Removes a source from the whitelist."""
        clean_id = str(identifier).strip()
        if not clean_id:
            return (False, "⚠️ Manba ko'rsatilmadi. Masalan: <code>/digest remove @kanal</code>")

        success = await self.db_manager.remove_digest_source(clean_id)
        if success:
            return (True, f"✅ <code>{html.escape(clean_id)}</code> kuzatiladigan manbalar ro'yxatidan o'chirildi.")
        else:
            return (False, f"❌ <code>{html.escape(clean_id)}</code> manbalar ro'yxatida topilmadi.")

    async def list_sources_summary(self) -> List[Dict[str, Any]]:
        """Lists active sources with their post counts."""
        sources = await self.db_manager.list_digest_sources(active_only=True)
        counts = await self.db_manager.get_all_source_post_counts()
        result = []
        for s in sources:
            result.append({
                "peer_id": s.peer_id,
                "title": s.title,
                "username": s.username,
                "peer_type": s.peer_type,
                "post_count": counts.get(s.peer_id, 0),
                "added_at": s.added_at,
            })
        return result

    async def collect_new_posts(self, client: Any = None, trigger: str = "manual") -> Dict[str, Any]:
        """
        Collects new posts from whitelisted sources since last_message_id.
        - Error isolation per source (one failing source never stops others)
        - Telethon FloodWait handling
        - Reading history does NOT mark messages as read
        - Empty posts are skipped
        - Truncates very long posts for AI input if needed, but keeps full text in DB
        - Saves to digest_posts and updates digest_state
        - Purges posts older than RETENTION_DAYS
        """
        telethon_client = client or self.get_client()
        if not telethon_client:
            logger.warning("collect_new_posts: Telethon client is not available.")
            return {
                "status": "error",
                "message": "Userbot mijozi ulanmagan.",
                "total_collected": 0,
                "sources_count": 0,
                "per_source": {},
                "errors": [],
            }

        sources = await self.db_manager.list_digest_sources(active_only=True)
        start_time = asyncio.get_event_loop().time()
        run_id = await self.db_manager.create_digest_run(trigger=trigger)

        stats: Dict[str, Any] = {
            "run_id": run_id,
            "status": "completed",
            "trigger": trigger,
            "total_collected": 0,
            "sources_count": len(sources),
            "per_source": {},
            "errors": [],
            "duration_seconds": 0.0,
            "ai_calls_made": 0,
        }

        for source in sources:
            if stats["total_collected"] >= settings.MAX_POSTS_PER_RUN:
                logger.info(
                    f"Reached MAX_POSTS_PER_RUN limit ({settings.MAX_POSTS_PER_RUN}). "
                    f"Prioritizing {len(stats['per_source'])} higher-priority sources."
                )
                stats["limit_reached"] = True
                break

            source_collected = 0
            source_status = "ok"
            try:
                last_msg_id = await self.db_manager.get_digest_state(source.peer_id)
                max_seen_id = last_msg_id
                posts_to_save: List[DigestPost] = []

                # Calculate lookback cutoff if last_msg_id is 0
                cutoff_time = datetime.now(timezone.utc) - timedelta(hours=settings.DIGEST_LOOKBACK_HOURS)

                # Fetch messages using iter_messages (Read-only, DOES NOT mark messages as read)
                iter_kwargs: Dict[str, Any] = {"limit": settings.MAX_POSTS_PER_RUN}
                if last_msg_id > 0:
                    iter_kwargs["min_id"] = last_msg_id

                async for msg in telethon_client.iter_messages(source.peer_id, **iter_kwargs):
                    if last_msg_id == 0 and getattr(msg, "date", None):
                        msg_date = msg.date
                        if msg_date.tzinfo is None:
                            msg_date = msg_date.replace(tzinfo=timezone.utc)
                        if msg_date < cutoff_time:
                            break

                    raw_msg = getattr(msg, "message", None)
                    if not isinstance(raw_msg, str):
                        raw_msg = getattr(msg, "text", None)
                    raw_text = raw_msg.strip() if isinstance(raw_msg, str) else ""
                    if not raw_text:
                        # Skip empty post (e.g. photos without caption, service messages)
                        continue

                    # Forward info
                    orig_source = None
                    fwd = getattr(msg, "forward", None)
                    if fwd:
                        chat = getattr(fwd, "chat", None)
                        sender = getattr(fwd, "sender", None)
                        from_name = getattr(fwd, "from_name", None)
                        if chat and isinstance(getattr(chat, "title", None), str):
                            orig_source = chat.title
                        elif sender and isinstance(getattr(sender, "first_name", None), str):
                            orig_source = sender.first_name
                        elif isinstance(from_name, str):
                            orig_source = from_name

                    msg_date_str = msg.date.isoformat() if getattr(msg, "date", None) else datetime.utcnow().isoformat()
                    link = self.build_post_link(source.peer_id, msg.id, source.username)

                    post = DigestPost(
                        chat_id=source.peer_id,
                        message_id=msg.id,
                        chat_title=source.title,
                        chat_username=source.username,
                        text=raw_text,  # Full text stored in DB
                        original_source=orig_source,
                        posted_at=msg_date_str,
                        link=link,
                        digest_run_id=run_id,
                        status="new",
                    )
                    posts_to_save.append(post)
                    if msg.id > max_seen_id:
                        max_seen_id = msg.id

                if posts_to_save:
                    source_collected = await self.db_manager.bulk_save_digest_posts(posts_to_save)
                    stats["total_collected"] += source_collected

                if max_seen_id > last_msg_id:
                    await self.db_manager.set_digest_state(source.peer_id, max_seen_id)

            except FloodWaitError as e:
                wait_sec = int(getattr(e, "seconds", 1))
                logger.warning(f"FloodWait on source {source.title} (ID {source.peer_id}): {wait_sec}s")
                source_status = f"flood_wait_{wait_sec}s"
                stats["errors"].append({
                    "peer_id": source.peer_id,
                    "title": source.title,
                    "error": f"FloodWait: {wait_sec}s",
                })
            except Exception as e:
                logger.error(f"Error collecting posts from source {source.title} (ID {source.peer_id}): {e}", exc_info=True)
                source_status = f"error: {str(e)}"
                stats["errors"].append({
                    "peer_id": source.peer_id,
                    "title": source.title,
                    "error": str(e),
                })

            stats["per_source"][str(source.peer_id)] = {
                "title": source.title,
                "collected": source_collected,
                "status": source_status,
            }

        # Retention cleanup: purge old posts
        purged = await self.db_manager.purge_old_digest_posts(settings.RETENTION_DAYS)
        stats["purged_old_posts"] = purged

        # Phase 2: Execute cleaning, deduplication, and spam filtering
        filter_stats = await self.clean_and_filter_run_posts(run_id=run_id)
        stats["cleaning"] = filter_stats
        stats["duplicates_filtered"] = filter_stats["duplicates_count"]
        stats["spam_filtered"] = filter_stats["spam_count"]
        stats["clean_remaining"] = filter_stats["clean_remaining"]
        stats["suspicious_jobs"] = filter_stats["suspicious_jobs_count"]

        # Phase 3: Execute classification on clean posts
        classification_stats = await self.classify_run_posts(run_id=run_id)
        stats["classification"] = {
            "topic_counts": classification_stats.get("topic_counts", {}),
            "total_classified": classification_stats.get("total_classified", 0),
            "total_discarded": classification_stats.get("total_discarded", 0),
            "failed_batches_count": classification_stats.get("failed_batches_count", 0),
            "skipped_posts_count": classification_stats.get("skipped_posts_count", 0),
            "ai_calls_made": classification_stats.get("ai_calls_made", 0),
        }
        stats["classified_count"] = classification_stats.get("total_classified", 0)
        stats["discarded_classified_count"] = classification_stats.get("total_discarded", 0)
        stats["skipped_posts_count"] = classification_stats.get("skipped_posts_count", 0)
        stats["failed_batches_count"] = classification_stats.get("failed_batches_count", 0)
        stats["topic_counts"] = classification_stats.get("topic_counts", {})

        # Phase 4: Execute summarization and anti-hallucination verification
        summary_stats = await self.summarize_run_posts(run_id=run_id)
        stats["summarization"] = {
            "topic_items_count": summary_stats.get("topic_items_count", {}),
            "total_items_selected": summary_stats.get("total_items_selected", 0),
            "discarded_by_verification_count": summary_stats.get("discarded_by_verification_count", 0),
            "failed_topics": summary_stats.get("failed_topics", []),
            "skipped_by_limit_count": summary_stats.get("skipped_by_limit_count", 0),
        }
        stats["sections"] = summary_stats.get("sections", {})
        stats["total_items_selected"] = summary_stats.get("total_items_selected", 0)
        stats["discarded_by_verification_count"] = summary_stats.get("discarded_by_verification_count", 0)
        stats["failed_topics"] = summary_stats.get("failed_topics", [])
        stats["skipped_by_limit_count"] = summary_stats.get("skipped_by_limit_count", 0)

        # Calculate execution duration
        duration_seconds = max(0.01, asyncio.get_event_loop().time() - start_time)
        stats["duration_seconds"] = duration_seconds
        stats["ai_calls_made"] = (
            classification_stats.get("ai_calls_made", 0)
            + summary_stats.get("ai_calls_made", 0)
        )

        # Finish run in DB with telemetry metrics
        run_status = "completed" if not stats["errors"] else ("partial" if stats["total_collected"] > 0 else "failed")
        await self.db_manager.finish_digest_run(
            run_id=run_id,
            status=run_status,
            stats=stats,
            duration_seconds=duration_seconds,
        )

        return stats

    async def clean_and_filter_run_posts(self, run_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Executes Phase 2 Cleaning & Filtering on unclassified posts:
        - Exact & Near-duplicate detection (merges all source links)
        - Spam/Scam/Ad scoring with reasons (Latin/Cyrillic/RU/EN)
        - Safelisting legitimate vacancies, courses, grants, competitions
        - Flagging suspicious job ads
        - Updates DB status: filtered vs new, records spam_score and discard_reason
        """
        posts = await self.db_manager.get_digest_posts(run_id=run_id, status="new", limit=settings.MAX_POSTS_PER_RUN)
        if not posts:
            return {
                "total_input": 0,
                "clean_remaining": 0,
                "duplicates_count": 0,
                "spam_count": 0,
                "suspicious_jobs_count": 0,
            }

        batch_result = self.cleaner.process_and_filter_batch(posts)

        # Batch update all posts in DB (clean, duplicates, spam)
        all_processed: List[DigestPost] = []
        all_processed.extend(batch_result["clean_posts"])
        all_processed.extend(batch_result["duplicate_posts"])
        all_processed.extend(batch_result["spam_posts"])

        await self.db_manager.bulk_update_digest_posts_filter_status(all_processed)

        return batch_result["stats"]

    async def classify_run_posts(self, run_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Executes Phase 3 Classification on clean posts:
        - Batches posts according to BATCH_SIZE
        - Respects MAX_AI_CALLS_PER_RUN quota limit
        - Sends to Gemini with temperature 0.0
        - Enforces strict JSON schema validation and retry-once logic
        - Topic taxonomy A-F and 'other' with precedence F > C > E > B > A > D
        - Discards 'other', importance <= 1, and advertisements
        - Extracts structured fields for Topic D and Topic F
        - Bulk updates database with classified topics, importance, and extracted JSON
        """
        posts = await self.db_manager.get_digest_posts(run_id=run_id, status="new", limit=settings.MAX_POSTS_PER_RUN)
        if not posts:
            return {
                "classified_posts": [],
                "discarded_posts": [],
                "topic_counts": {},
                "ai_calls_made": 0,
                "failed_batches_count": 0,
                "skipped_posts_count": 0,
                "total_classified": 0,
                "total_discarded": 0,
            }

        res = await self.classifier.classify_posts(posts)

        # Bulk update classified and discarded posts in DB
        all_processed: List[DigestPost] = []
        all_processed.extend(res["classified_posts"])
        all_processed.extend(res["discarded_posts"])

        await self.db_manager.bulk_update_digest_posts_classification(all_processed)

        return res

    async def summarize_run_posts(self, run_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Executes Phase 4 Event Grouping, Summarization, and Verification:
        - Fetches classified posts with status='classified'
        - Groups posts by event within each topic
        - Summarizes via Gemini (temperature 0.0, strict JSON)
        - Verifies every item by code via DigestVerifier (numbers, proper entities, URLs, phones, post_ids)
        - Formats Section D deterministically from extracted fields
        - Sorts Section F by nearest deadline
        - Updates database with approved (status='included') and discarded posts
        """
        posts = await self.db_manager.get_digest_posts(run_id=run_id, status="classified", limit=settings.MAX_POSTS_PER_RUN)
        if not posts:
            return {
                "sections": {},
                "topic_items_count": {},
                "discarded_by_verification": [],
                "discarded_by_verification_count": 0,
                "failed_topics": [],
                "skipped_by_limit_count": 0,
                "raw_posts_analyzed": 0,
                "total_items_selected": 0,
            }

        res = await self.summarizer.process_all_topics(posts)

        # Update posts status in database
        posts_to_update: List[DigestPost] = []
        posts_by_id = {p.id: p for p in posts if p.id is not None}

        # Mark included posts
        for items in res["sections"].values():
            for item in items:
                for pid in item.post_ids_used:
                    if pid in posts_by_id:
                        p = posts_by_id[pid]
                        p.status = "included"
                        p.discard_reason = None
                        posts_to_update.append(p)

        # Add discarded posts
        posts_to_update.extend(res["discarded_by_verification"])

        if posts_to_update:
            await self.db_manager.bulk_update_digest_posts_classification(posts_to_update)

        return res

    async def run_smart_digest(
        self,
        trigger: str = "manual",
        topic_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes full Smart Digest V2 pipeline with concurrency guard:
        1. Checks concurrency lock: if running, returns 'in_progress' status
        2. Collects new posts from whitelisted sources
        3. Cleans, transliterates, deduplicates, and filters spam
        4. Classifies topics A-F with precedence rules
        5. Groups events, creates summaries, and runs code verification
        6. Formats Telegram HTML messages under 4000 characters
        """
        if self._is_run_in_progress:
            return {
                "status": "in_progress",
                "message": "⚠️ <b>Smart Digest V2 hozirda tayyorlanmoqda!</b> Iltimos, jarayon tugashini kuting.",
                "messages": ["⚠️ <b>Smart Digest V2 hozirda tayyorlanmoqda!</b> Iltimos, jarayon tugashini kuting."],
            }

        self._is_run_in_progress = True
        try:
            # 1. Run collection, cleaning, classification, and summarization
            stats = await self.collect_new_posts(trigger=trigger)

            # Check if sources exist
            if stats.get("sources_count", 0) == 0:
                empty_msg = (
                    "ℹ️ <b>Kuzatiladigan manbalar mavjud emas!</b>\n\n"
                    "Smart Digest V2 xabarlarni faqat siz ko'rsatgan kanallar va guruhlardan yig'adi.\n\n"
                    "Qo'shish: <code>/digest add @kanal</code>"
                )
                return {
                    "status": "no_sources",
                    "stats": stats,
                    "messages": [empty_msg],
                }

            run_id = stats.get("run_id")
            sections = stats.get("sections", {})
            selected_count = stats.get("total_items_selected", 0)

            # Fallback if sections were not populated in stats
            if not sections and run_id:
                included_posts = await self.db_manager.get_digest_posts(
                    run_id=run_id, status="included", limit=settings.MAX_POSTS_PER_RUN
                )
                if included_posts:
                    processed_sum = await self.summarizer.process_all_topics(included_posts)
                    sections = processed_sum.get("sections", {})
                    selected_count = processed_sum.get("total_items_selected", 0)

            if topic_filter:
                clean_topic = topic_filter.strip().upper()
                items = sections.get(clean_topic, [])
                formatted_messages = self.formatter.format_single_topic(
                    topic=clean_topic,
                    items=items,
                    analyzed_count=stats.get("total_collected", 0),
                )
            else:
                formatted_messages = self.formatter.format_full_digest(
                    sections=sections,
                    analyzed_count=stats.get("total_collected", 0),
                    selected_count=selected_count,
                    spam_count=stats.get("spam_filtered", 0),
                    discarded_by_verification=stats.get("discarded_by_verification_count", 0),
                    skipped_by_limit=stats.get("skipped_by_limit_count", 0),
                    failed_batches=stats.get("failed_batches_count", 0),
                    failed_topics=stats.get("failed_topics", []),
                )

            return {
                "status": "completed",
                "stats": stats,
                "sections": sections,
                "messages": formatted_messages,
                "run_id": run_id,
            }
        except Exception as e:
            logger.error(f"Error in run_smart_digest: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "messages": [f"❌ <b>Digest tayyorlashda xatolik yuz berdi:</b>\n<code>{html.escape(str(e))}</code>"],
            }
        finally:
            self._is_run_in_progress = False
