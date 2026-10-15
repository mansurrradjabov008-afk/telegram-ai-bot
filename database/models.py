"""
Data models and dataclasses for SQLite persistence.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Contact:
    user_id: int
    username: Optional[str]
    full_name: str
    relationship_type: str = "general"  # yaqin_dost, rasmiy, hamkor, ustoz, general
    addressing_style: str = "siz"  # "siz" (hurmat bilan) or "sen" (yaqin do'st / tengdosh)
    mode: str = "auto"  # "auto", "draft", "mute"
    custom_instructions: Optional[str] = None
    language_code: str = "uz"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Draft:
    id: Optional[int]
    contact_username: Optional[str]
    contact_user_id: Optional[int]
    source_message_id: int
    incoming_text: str
    proposed_reply: str
    status: str = "pending"  # pending, sent, rejected, edited_sent, expired, superseded
    is_sensitive: bool = False
    sensitive_reason: Optional[str] = None
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
    expires_at: str = ""

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def user_id(self) -> Optional[int]:
        return self.contact_user_id

    @property
    def draft_text(self) -> str:
        return self.proposed_reply


@dataclass
class SchemaVersion:
    version: int
    description: str
    applied_at: Optional[str] = None


@dataclass
class ScheduledTask:
    id: Optional[int]
    target_peer: str  # @username or phone or user_id
    message_text: str
    scheduled_time: str  # ISO 8601 string
    status: str = "pending"  # pending, executed, cancelled, failed
    error_message: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class ConversationMessage:
    id: Optional[int]
    chat_id: int
    sender_name: str
    role: str  # contact, assistant, boss
    content: str
    timestamp: str


@dataclass
class MonitoredSource:
    peer_id: int
    title: str
    peer_type: str  # channel or group
    is_active: bool = True
    keywords: Optional[str] = None  # comma-separated keywords
    created_at: Optional[str] = None


@dataclass
class DigestSource:
    peer_id: int
    title: str
    username: Optional[str] = None
    peer_type: str = "channel"  # channel or group
    is_active: bool = True
    added_at: Optional[str] = None
    priority: int = 1


@dataclass
class DigestPost:
    chat_id: int = 0
    message_id: int = 0
    chat_title: str = ""
    text: str = ""
    id: Optional[int] = None
    chat_username: Optional[str] = None
    original_source: Optional[str] = None
    posted_at: Optional[str] = None
    link: Optional[str] = None
    normalized_hash: Optional[str] = None
    fetched_at: Optional[str] = None
    spam_score: float = 0.0
    spam_reasons: Optional[str] = None
    topic: Optional[str] = None
    importance: int = 1
    status: str = "new"  # new, filtered, classified, included, discarded
    discard_reason: Optional[str] = None
    digest_run_id: Optional[int] = None
    extracted_json: Optional[str] = None


@dataclass
class DigestRun:
    id: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    trigger: str = "manual"  # manual or scheduled
    status: str = "in_progress"  # in_progress, completed, failed
    stats_json: Optional[str] = None
    posts_collected: int = 0
    posts_filtered: int = 0
    posts_classified: int = 0
    items_in_digest: int = 0
    ai_calls_count: int = 0
    duration_seconds: float = 0.0


@dataclass
class DigestState:
    chat_id: int
    last_message_id: int
    updated_at: Optional[str] = None


# Contact Memory Models
@dataclass
class MemContact:
    contact_user_id: int
    username_at_bind: Optional[str] = None
    memory_enabled: int = 1
    paused_reason: Optional[str] = None
    bound_at: Optional[str] = None


@dataclass
class MemMessage:
    id: Optional[int]
    contact_user_id: int
    tg_message_id: int
    sender: str  # 'contact', 'owner_manual', 'owner_edited', 'bot_approved', 'bot_auto'
    text_redacted: str
    sent_at: Optional[str] = None
    edited_at: Optional[str] = None
    is_media: int = 0


@dataclass
class MemFact:
    id: Optional[int]
    contact_user_id: int
    category: Optional[str]
    text: str
    source_type: str  # 'owner_stated', 'contact_claim', 'owner_confirmed'
    source_message_ids: Optional[str] = None  # JSON array string
    status: str = "active"  # 'active', 'superseded', 'disputed'
    confidence: float = 1.0
    supersedes_id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    valid_until: Optional[str] = None
    injection_suspected: int = 0


@dataclass
class MemOpenItem:
    id: Optional[int]
    contact_user_id: int
    text: str
    source_message_ids: Optional[str] = None
    status: str = "open"  # 'open', 'resolved', 'dismissed'
    created_at: Optional[str] = None


@dataclass
class MemSummary:
    id: Optional[int]
    contact_user_id: int
    summary_text: str
    covers_until_message_id: Optional[int] = None
    version: int = 1
    created_at: Optional[str] = None


@dataclass
class MemEmbedding:
    id: Optional[int]
    contact_user_id: int
    item_type: str  # 'fact', 'message', 'summary'
    item_id: int
    model_name: str
    dim: int
    vector: bytes
    created_at: Optional[str] = None


@dataclass
class RadarMention:
    id: Optional[int]
    group_id: int
    group_title: str
    message_id: int
    sender_id: int
    sender_name: str
    sender_username: Optional[str]
    message_text: str
    trigger_type: str  # 'mention', 'reply', 'name_keyword', 'custom_keyword'
    detected_at: Optional[str] = None

