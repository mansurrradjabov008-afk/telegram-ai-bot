"""
Database connection and schema initialization using aiosqlite.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
import aiosqlite
from config import settings


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    relationship_type TEXT DEFAULT 'general',
    addressing_style TEXT DEFAULT 'siz', -- 'siz' (hurmat) or 'sen' (dostona/yaqin)
    custom_instructions TEXT,
    language_code TEXT DEFAULT 'uz',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contacts_username ON contacts(username);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_peer TEXT NOT NULL,
    message_text TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON scheduled_tasks(status);

CREATE TABLE IF NOT EXISTS conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conv_chat_id ON conversation_history(chat_id);

CREATE TABLE IF NOT EXISTS monitored_sources (
    peer_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    peer_type TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    keywords TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_feed_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_peer_id INTEGER NOT NULL,
    source_title TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    post_text TEXT NOT NULL,
    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_summarized INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_feed_unsummarized ON raw_feed_posts(is_summarized);
"""


@asynccontextmanager
async def get_db(db_path: Path = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Provides an asynchronous context-managed connection to SQLite database."""
    path = db_path or settings.db_file_path
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        yield db


async def init_db(db_path: Path = None):
    """Initializes tables, indexes, and applies non-destructive migrations."""
    path = db_path or settings.db_file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(DB_SCHEMA)
        # Migration: ensure addressing_style exists on existing tables
        try:
            await db.execute("ALTER TABLE contacts ADD COLUMN addressing_style TEXT DEFAULT 'siz'")
        except Exception:
            pass
        await db.commit()
