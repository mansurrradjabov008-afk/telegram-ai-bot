"""
Group Radar Service (Shaxsiy Guruh Kuzatuvchisi).
Monitors Telegram groups for mentions, replies, and name/keyword references
to keep Mansur informed without group spamming or unnecessary noise.
"""

import html
import logging
import re
import time
from typing import Optional, Callable, Dict, Any, List, Set
from telethon.tl.types import User, Channel, Chat

from config import settings
from database.crud import DatabaseManager
from database.models import RadarMention

logger = logging.getLogger(__name__)

# Name pattern matching: mansur, mansurjon, mansurbek, mansuraka, radjabov, rajabov, radzhabov
# along with possible suffixes like -ga, -da, -dan, -ni, -ning, -cha, etc.
DEFAULT_NAME_REGEX = re.compile(
    r"(?i)(?:^|(?<=\W))(?:@?mansurjon_07|mansur(?:jon|bek|aka|boy|chik|im)?|ra(?:dj|j|dzh)abov)(?:[-']?(?:ga|da|dan|ni|ning|mi|ku|chi|dir|lar|miz|siz))?\b",
    re.IGNORECASE,
)


def generate_jump_link(chat_username: Optional[str], chat_id: int, message_id: int) -> str:
    """Generates direct Telegram jump link to a specific message in a group."""
    if chat_username:
        uname = chat_username.lstrip("@")
        return f"https://t.me/{uname}/{message_id}"

    clean_id = str(chat_id)
    if clean_id.startswith("-100"):
        clean_id = clean_id[4:]
    elif clean_id.startswith("-"):
        clean_id = clean_id[1:]
    return f"https://t.me/c/{clean_id}/{message_id}"


class GroupRadarService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        ai_engine: Optional[Any] = None,
        name_regex: Optional[re.Pattern] = None,
    ):
        self.db = db_manager
        self.ai_engine = ai_engine
        self.name_regex = name_regex or DEFAULT_NAME_REGEX

        self._alert_callback: Optional[Callable] = None
        self._muted_group_ids: Set[int] = set()
        self._muted_cache_loaded: bool = False

        # Debounce tracking per group_id: {group_id: {"alert_msg_id": int, "timestamp": float, "count": int, "last_text": str}}
        self._recent_alerts: Dict[int, Dict[str, Any]] = {}
        self.debounce_seconds = getattr(settings, "RADAR_DEBOUNCE_SECONDS", 90)

    def set_alert_callback(self, callback: Callable):
        """Sets the callback to notify Admin Bot Cockpit with radar alerts."""
        self._alert_callback = callback

    async def _ensure_muted_cache(self):
        """Loads muted groups from database once on startup."""
        if not self._muted_cache_loaded:
            try:
                muted_groups = await self.db.list_radar_muted_groups()
                self._muted_group_ids = {g["group_id"] for g in muted_groups}
                self._muted_cache_loaded = True
            except Exception as e:
                logger.warning(f"Failed to load muted groups cache: {type(e).__name__}")

    async def is_group_muted(self, group_id: int) -> bool:
        """Checks whether the specified group is muted in radar."""
        await self._ensure_muted_cache()
        if group_id in self._muted_group_ids:
            return True
        return await self.db.is_radar_group_muted(group_id)

    async def mute_group(self, group_id: int, title: str = "") -> bool:
        """Mutes a group and updates the in-memory cache."""
        success = await self.db.mute_radar_group(group_id, title)
        if success:
            self._muted_group_ids.add(group_id)
        return success

    async def unmute_group(self, group_id: int) -> bool:
        """Unmutes a group and updates the in-memory cache."""
        success = await self.db.unmute_radar_group(group_id)
        if success and group_id in self._muted_group_ids:
            self._muted_group_ids.remove(group_id)
        return success

    def check_text_trigger(self, text: str, custom_keywords: List[str]) -> Optional[Dict[str, str]]:
        """
        Scans message text for owner's name, variations, or custom keywords.
        Returns dict with trigger info if matched, None otherwise.
        """
        if not text:
            return None

        # Check name regex
        m = self.name_regex.search(text)
        if m:
            matched_word = m.group(0)
            return {
                "trigger_type": "name_keyword",
                "trigger_label": f"Ismingiz eslandi (\"{matched_word}\")",
                "keyword": matched_word,
            }

        # Check custom keywords
        for kw in custom_keywords:
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue
            pattern = rf"(?i)\b{re.escape(kw_clean)}\b"
            if re.search(pattern, text):
                return {
                    "trigger_type": "custom_keyword",
                    "trigger_label": f"Kalit so'z topildi (\"{kw_clean}\")",
                    "keyword": kw_clean,
                }

        return None

    async def inspect_event(self, event, client) -> Optional[Dict[str, Any]]:
        """
        Inspects an incoming Telethon event in a group to determine if it triggers radar.
        Returns detection dictionary or None if ignored.
        """
        if not getattr(settings, "RADAR_ENABLED", True):
            return None

        # Must be in a group/supergroup
        if not getattr(event, "is_group", False):
            return None

        # Ignore own messages
        if getattr(event, "out", False):
            return None

        # Ignore service messages
        if getattr(event.message, "action", None) is not None:
            return None

        sender = await event.get_sender()
        if not sender:
            return None

        # Ignore bots and self
        if getattr(sender, "bot", False) or getattr(sender, "is_self", False):
            return None

        chat = await event.get_chat()
        group_id = getattr(chat, "id", None) or getattr(event, "chat_id", 0)
        if not group_id:
            return None

        # Check muted
        if await self.is_group_muted(group_id):
            return None

        message_text = (event.raw_text or "").strip()
        message_id = getattr(event, "id", 0)
        group_title = getattr(chat, "title", "Guruh")
        group_username = getattr(chat, "username", None)

        sender_id = getattr(sender, "id", 0)
        sender_first = getattr(sender, "first_name", "") or ""
        sender_last = getattr(sender, "last_name", "") or ""
        sender_name = f"{sender_first} {sender_last}".strip() or "Foydalanuvchi"
        sender_username = getattr(sender, "username", None)

        trigger_type = None
        trigger_label = None

        # Trigger 1: Direct @mention in group
        if getattr(event, "mentioned", False):
            trigger_type = "direct_mention"
            trigger_label = "To'g'ridan-to'g'ri chaqiruv (@tag)"

        # Trigger 2: Reply to owner's message
        if not trigger_type and getattr(event, "is_reply", False):
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg:
                    me = await client.get_me()
                    if getattr(reply_msg, "out", False) or getattr(reply_msg, "sender_id", 0) == getattr(me, "id", 0):
                        trigger_type = "reply_to_owner"
                        trigger_label = "Sizning xabaringizga javob"
            except Exception as e:
                logger.debug(f"Failed to check reply message in radar: {type(e).__name__}")

        # Trigger 3 & 4: Name variations or custom keywords in text
        if not trigger_type and message_text:
            # Combine static config keywords and dynamic DB keywords
            db_keywords = await self.db.list_radar_keywords()
            config_keywords = getattr(settings, "RADAR_KEYWORDS", [])
            all_keywords = list(set([k.lower() for k in config_keywords + db_keywords if k]))

            text_trigger = self.check_text_trigger(message_text, all_keywords)
            if text_trigger:
                trigger_type = text_trigger["trigger_type"]
                trigger_label = text_trigger["trigger_label"]

        if not trigger_type:
            return None

        jump_link = generate_jump_link(group_username, group_id, message_id)

        return {
            "group_id": group_id,
            "group_title": group_title,
            "group_username": group_username,
            "message_id": message_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "sender_username": sender_username,
            "message_text": message_text,
            "trigger_type": trigger_type,
            "trigger_label": trigger_label,
            "jump_link": jump_link,
        }

    async def handle_group_message(self, event, client):
        """
        Full handler pipeline for incoming group messages.
        Inspects, logs to database, applies sliding window debounce,
        and triggers Admin Bot Cockpit notification.
        """
        try:
            detected = await self.inspect_event(event, client)
            if not detected:
                return

            group_id = detected["group_id"]
            message_id = detected["message_id"]

            # Log to DB
            await self.db.log_radar_mention(
                group_id=group_id,
                group_title=detected["group_title"],
                message_id=message_id,
                sender_id=detected["sender_id"],
                sender_name=detected["sender_name"],
                sender_username=detected["sender_username"],
                message_text=detected["message_text"],
                trigger_type=detected["trigger_type"],
            )

            # Run Deep AI Cognitive Analysis
            if self.ai_engine and hasattr(self.ai_engine, "analyze_radar_mention"):
                try:
                    ai_res = await self.ai_engine.analyze_radar_mention(
                        group_title=detected["group_title"],
                        sender_name=detected["sender_name"],
                        message_text=detected["message_text"],
                        trigger_type=detected["trigger_type"],
                    )
                    if isinstance(ai_res, dict):
                        if not ai_res.get("is_relevant", True):
                            logger.info(f"Radar ignored false-positive mention in {detected['group_title']}")
                            return
                        detected["ai_summary"] = ai_res.get("summary")
                        detected["urgency_label"] = ai_res.get("urgency_label")
                        detected["suggested_reply"] = ai_res.get("suggested_reply")
                except Exception as ae:
                    logger.debug(f"AI radar analysis skipped: {type(ae).__name__}")

            if not self._alert_callback:
                return

            now = time.time()
            recent = self._recent_alerts.get(group_id)

            existing_msg_id = None
            is_debounced = False

            if recent and (now - recent["timestamp"] < self.debounce_seconds):
                # Within debounce sliding window
                existing_msg_id = recent.get("alert_msg_id")
                recent["count"] += 1
                recent["timestamp"] = now
                recent["last_text"] = detected["message_text"]
                detected["mentions_count"] = recent["count"]
                is_debounced = True
            else:
                detected["mentions_count"] = 1

            # Dispatch alert callback
            alert_result = await self._alert_callback(
                detected=detected,
                existing_alert_msg_id=existing_msg_id if is_debounced else None,
            )

            # Record or update recent alert info
            if alert_result and isinstance(alert_result, int):
                self._recent_alerts[group_id] = {
                    "alert_msg_id": alert_result,
                    "timestamp": now,
                    "count": detected["mentions_count"],
                    "last_text": detected["message_text"],
                }

        except Exception as e:
            logger.error(f"Error handling group radar message: {type(e).__name__}")
