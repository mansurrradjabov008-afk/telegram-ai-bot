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
    mode: str = "draft"  # "auto", "draft", "mute"
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


@dataclass
class DigestState:
    chat_id: int
    last_message_id: int
    updated_at: Optional[str] = None
