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
    custom_instructions: Optional[str] = None
    language_code: str = "uz"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


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
