"""
Database connection and schema initialization using aiosqlite.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator
import aiosqlite
from config import settings


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contacts (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    relationship_type TEXT DEFAULT 'general',
    addressing_style TEXT DEFAULT 'siz', -- 'siz' (hurmat) or 'sen' (dostona/yaqin)
    mode TEXT DEFAULT 'draft' CHECK(mode IN ('auto', 'draft', 'mute')),
    custom_instructions TEXT,
    language_code TEXT DEFAULT 'uz',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contacts_username ON contacts(username);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_username TEXT,
    contact_user_id INTEGER,
    source_message_id INTEGER NOT NULL,
    incoming_text TEXT NOT NULL,
    proposed_reply TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sent', 'rejected', 'edited_sent', 'expired', 'superseded')),
    is_sensitive INTEGER NOT NULL DEFAULT 0,
    sensitive_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP,
    expires_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_expires ON drafts(expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_contact_source_user 
ON drafts(contact_user_id, source_message_id) 
WHERE contact_user_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_contact_source_uname 
ON drafts(contact_username, source_message_id) 
WHERE contact_user_id IS NULL AND contact_username IS NOT NULL;

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

-- Smart Digest V2 Tables
CREATE TABLE IF NOT EXISTS digest_sources (
    peer_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    peer_type TEXT NOT NULL, -- 'channel' or 'group'
    is_active INTEGER DEFAULT 1,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS digest_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    chat_title TEXT NOT NULL,
    chat_username TEXT,
    text TEXT NOT NULL,
    original_source TEXT,
    posted_at TIMESTAMP,
    link TEXT,
    normalized_hash TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    spam_score REAL DEFAULT 0,
    spam_reasons TEXT,
    topic TEXT,
    importance INTEGER DEFAULT 1,
    status TEXT DEFAULT 'new' CHECK(status IN ('new', 'filtered', 'classified', 'included', 'discarded')),
    discard_reason TEXT,
    digest_run_id INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_posts_chat_msg ON digest_posts(chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_digest_posts_status ON digest_posts(status);
CREATE INDEX IF NOT EXISTS idx_digest_posts_posted_at ON digest_posts(posted_at);
CREATE INDEX IF NOT EXISTS idx_digest_posts_run_id ON digest_posts(digest_run_id);

CREATE TABLE IF NOT EXISTS digest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    trigger TEXT DEFAULT 'manual' CHECK(trigger IN ('manual', 'scheduled')),
    status TEXT DEFAULT 'in_progress' CHECK(status IN ('in_progress', 'completed', 'failed', 'partial')),
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS digest_state (
    chat_id INTEGER PRIMARY KEY,
    last_message_id INTEGER NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
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
    """Initializes tables, indexes, and applies non-destructive, idempotent migrations."""
    path = db_path or settings.db_file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.executescript(DB_SCHEMA)

        # Baseline schema version
        await db.execute(
            "INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?, ?)",
            (1, "Baseline schema initialization"),
        )

        # Inspect existing contacts table columns for safe migration
        async with db.execute("PRAGMA table_info(contacts)") as cur:
            cols = [row["name"] for row in await cur.fetchall()]

        # Migration: ensure addressing_style exists on existing tables
        if "addressing_style" not in cols:
            try:
                await db.execute("ALTER TABLE contacts ADD COLUMN addressing_style TEXT DEFAULT 'siz'")
                await db.execute(
                    "INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?, ?)",
                    (2, "Add addressing_style column"),
                )
            except Exception:
                pass

        # Migration: ensure mode exists on existing tables with CHECK constraint
        if "mode" not in cols:
            await db.execute(
                "ALTER TABLE contacts ADD COLUMN mode TEXT DEFAULT 'draft' CHECK(mode IN ('auto', 'draft', 'mute'))"
            )

        # Migrate existing contacts to configured MIGRATE_EXISTING_TO constant if NULL
        migrate_target = getattr(settings, "MIGRATE_EXISTING_TO", "draft") or "draft"
        if migrate_target not in ("auto", "draft", "mute"):
            migrate_target = "draft"

        await db.execute("UPDATE contacts SET mode = ? WHERE mode IS NULL", (migrate_target,))
        await db.execute("CREATE INDEX IF NOT EXISTS idx_contacts_mode ON contacts(mode);")

        # Record migration version 3
        await db.execute(
            "INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?, ?)",
            (3, "Add contact autonomy mode and drafts table"),
        )

        # Migration version 4: Smart Digest V2 schema
        await db.execute("""
        CREATE TABLE IF NOT EXISTS digest_sources (
            peer_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            peer_type TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS digest_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            chat_title TEXT NOT NULL,
            chat_username TEXT,
            text TEXT NOT NULL,
            original_source TEXT,
            posted_at TIMESTAMP,
            link TEXT,
            normalized_hash TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            spam_score REAL DEFAULT 0,
            spam_reasons TEXT,
            topic TEXT,
            importance INTEGER DEFAULT 1,
            status TEXT DEFAULT 'new' CHECK(status IN ('new', 'filtered', 'classified', 'included', 'discarded')),
            discard_reason TEXT,
            digest_run_id INTEGER
        );
        """)
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_posts_chat_msg ON digest_posts(chat_id, message_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_digest_posts_status ON digest_posts(status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_digest_posts_posted_at ON digest_posts(posted_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_digest_posts_run_id ON digest_posts(digest_run_id);")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS digest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            trigger TEXT DEFAULT 'manual' CHECK(trigger IN ('manual', 'scheduled')),
            status TEXT DEFAULT 'in_progress' CHECK(status IN ('in_progress', 'completed', 'failed', 'partial')),
            stats_json TEXT
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS digest_state (
            chat_id INTEGER PRIMARY KEY,
            last_message_id INTEGER NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        await db.execute(
            "INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?, ?)",
            (4, "Add Smart Digest V2 schema (digest_sources, digest_posts, digest_runs, digest_state)"),
        )

        await db.commit()
