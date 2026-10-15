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
    mode TEXT DEFAULT 'auto' CHECK(mode IN ('auto', 'draft', 'mute')),
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
    digest_run_id INTEGER,
    extracted_json TEXT
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

-- Contact Memory Tables (Schema v7)
CREATE TABLE IF NOT EXISTS mem_contacts (
    contact_user_id INTEGER PRIMARY KEY,
    username_at_bind TEXT,
    memory_enabled INTEGER DEFAULT 1,
    paused_reason TEXT,
    bound_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (contact_user_id) REFERENCES contacts(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mem_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_user_id INTEGER NOT NULL,
    tg_message_id INTEGER NOT NULL,
    sender TEXT NOT NULL CHECK(sender IN ('contact', 'owner_manual', 'owner_edited', 'bot_approved', 'bot_auto')),
    text_redacted TEXT NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    edited_at TIMESTAMP,
    is_media INTEGER DEFAULT 0,
    UNIQUE(contact_user_id, tg_message_id)
);

CREATE INDEX IF NOT EXISTS idx_mem_messages_contact ON mem_messages(contact_user_id);
CREATE INDEX IF NOT EXISTS idx_mem_messages_sent_at ON mem_messages(sent_at);

CREATE TABLE IF NOT EXISTS mem_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_user_id INTEGER NOT NULL,
    category TEXT,
    text TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('owner_stated', 'contact_claim', 'owner_confirmed')),
    source_message_ids TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'disputed')),
    confidence REAL DEFAULT 1.0,
    supersedes_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    valid_until TIMESTAMP,
    injection_suspected INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mem_facts_contact ON mem_facts(contact_user_id);
CREATE INDEX IF NOT EXISTS idx_mem_facts_status ON mem_facts(status);

CREATE TABLE IF NOT EXISTS mem_open_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    source_message_ids TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved', 'dismissed')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mem_open_items_contact ON mem_open_items(contact_user_id);
CREATE INDEX IF NOT EXISTS idx_mem_open_items_status ON mem_open_items(status);

CREATE TABLE IF NOT EXISTS mem_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_user_id INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    covers_until_message_id INTEGER,
    version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mem_summaries_contact ON mem_summaries(contact_user_id);

CREATE TABLE IF NOT EXISTS mem_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_user_id INTEGER NOT NULL,
    item_type TEXT NOT NULL CHECK(item_type IN ('fact', 'message', 'summary')),
    item_id INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mem_embeddings_contact ON mem_embeddings(contact_user_id);
CREATE INDEX IF NOT EXISTS idx_mem_embeddings_item ON mem_embeddings(item_type, item_id);

CREATE TABLE IF NOT EXISTS mem_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Contact Memory FTS5 Virtual Tables (Schema v8)
CREATE VIRTUAL TABLE IF NOT EXISTS mem_messages_fts USING fts5(
    text_redacted,
    content='mem_messages',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_mem_messages_ai AFTER INSERT ON mem_messages BEGIN
    INSERT INTO mem_messages_fts(rowid, text_redacted) VALUES (new.id, new.text_redacted);
END;

CREATE TRIGGER IF NOT EXISTS trg_mem_messages_ad AFTER DELETE ON mem_messages BEGIN
    INSERT INTO mem_messages_fts(mem_messages_fts, rowid, text_redacted) VALUES('delete', old.id, old.text_redacted);
END;

CREATE TRIGGER IF NOT EXISTS trg_mem_messages_au AFTER UPDATE ON mem_messages BEGIN
    INSERT INTO mem_messages_fts(mem_messages_fts, rowid, text_redacted) VALUES('delete', old.id, old.text_redacted);
    INSERT INTO mem_messages_fts(rowid, text_redacted) VALUES (new.id, new.text_redacted);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS mem_facts_fts USING fts5(
    text,
    content='mem_facts',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_mem_facts_ai AFTER INSERT ON mem_facts BEGIN
    INSERT INTO mem_facts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS trg_mem_facts_ad AFTER DELETE ON mem_facts BEGIN
    INSERT INTO mem_facts_fts(mem_facts_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS trg_mem_facts_au AFTER UPDATE ON mem_facts BEGIN
    INSERT INTO mem_facts_fts(mem_facts_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO mem_facts_fts(rowid, text) VALUES (new.id, new.text);
END;

-- Group Radar Tables (Schema v11)
CREATE TABLE IF NOT EXISTS radar_muted_groups (
    group_id INTEGER PRIMARY KEY,
    title TEXT,
    muted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS radar_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    group_title TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    sender_name TEXT NOT NULL,
    sender_username TEXT,
    message_text TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_radar_mentions_group ON radar_mentions(group_id);
CREATE INDEX IF NOT EXISTS idx_radar_mentions_time ON radar_mentions(detected_at);

CREATE TABLE IF NOT EXISTS radar_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@asynccontextmanager
async def get_db(db_path: Path = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Provides an asynchronous context-managed connection to SQLite database."""
    path = db_path or settings.db_file_path
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA busy_timeout = 5000;")
        await db.execute("PRAGMA synchronous = NORMAL;")
        await db.execute("PRAGMA cache_size = -64000;")
        await db.execute("PRAGMA mmap_size = 268435456;")
        await db.execute("PRAGMA temp_store = MEMORY;")
        yield db


async def init_db(db_path: Path = None):
    """Initializes tables, indexes, and applies non-destructive, idempotent migrations."""
    path = db_path or settings.db_file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA busy_timeout = 5000;")
        await db.execute("PRAGMA synchronous = NORMAL;")
        await db.execute("PRAGMA cache_size = -64000;")
        await db.execute("PRAGMA mmap_size = 268435456;")
        await db.execute("PRAGMA temp_store = MEMORY;")
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
            digest_run_id INTEGER,
            extracted_json TEXT
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

        # Migration version 5: Add extracted_json column to digest_posts if missing
        async with db.execute("PRAGMA table_info(digest_posts)") as cur:
            digest_cols = [row["name"] for row in await cur.fetchall()]

        if "extracted_json" not in digest_cols:
            try:
                await db.execute("ALTER TABLE digest_posts ADD COLUMN extracted_json TEXT")
            except Exception:
                pass

        await db.execute(
            "INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?, ?)",
            (5, "Add extracted_json column to digest_posts for Phase 3 classification"),
        )

        # Migration version 6: Add telemetry metrics to digest_runs and priority to digest_sources
        async with db.execute("PRAGMA table_info(digest_runs)") as cur:
            runs_cols = [row["name"] for row in await cur.fetchall()]

        for col_name, col_type in [
            ("posts_collected", "INTEGER DEFAULT 0"),
            ("posts_filtered", "INTEGER DEFAULT 0"),
            ("posts_classified", "INTEGER DEFAULT 0"),
            ("items_in_digest", "INTEGER DEFAULT 0"),
            ("ai_calls_count", "INTEGER DEFAULT 0"),
            ("duration_seconds", "REAL DEFAULT 0.0"),
        ]:
            if col_name not in runs_cols:
                try:
                    await db.execute(f"ALTER TABLE digest_runs ADD COLUMN {col_name} {col_type}")
                except Exception:
                    pass

        async with db.execute("PRAGMA table_info(digest_sources)") as cur:
            src_cols = [row["name"] for row in await cur.fetchall()]

        if "priority" not in src_cols:
            try:
                await db.execute("ALTER TABLE digest_sources ADD COLUMN priority INTEGER DEFAULT 1")
            except Exception:
                pass

        await db.execute(
            "INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?, ?)",
            (6, "Add telemetry metrics columns to digest_runs and priority to digest_sources"),
        )

        # Migration version 7: Contact Memory schema
        await db.execute("""
        CREATE TABLE IF NOT EXISTS mem_contacts (
            contact_user_id INTEGER PRIMARY KEY,
            username_at_bind TEXT,
            memory_enabled INTEGER DEFAULT 1,
            paused_reason TEXT,
            bound_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contact_user_id) REFERENCES contacts(user_id) ON DELETE CASCADE
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS mem_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_user_id INTEGER NOT NULL,
            tg_message_id INTEGER NOT NULL,
            sender TEXT NOT NULL CHECK(sender IN ('contact', 'owner_manual', 'owner_edited', 'bot_approved', 'bot_auto')),
            text_redacted TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            edited_at TIMESTAMP,
            is_media INTEGER DEFAULT 0,
            UNIQUE(contact_user_id, tg_message_id)
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_messages_contact ON mem_messages(contact_user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_messages_sent_at ON mem_messages(sent_at);")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS mem_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_user_id INTEGER NOT NULL,
            category TEXT,
            text TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK(source_type IN ('owner_stated', 'contact_claim', 'owner_confirmed')),
            source_message_ids TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'disputed')),
            confidence REAL DEFAULT 1.0,
            supersedes_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            valid_until TIMESTAMP,
            injection_suspected INTEGER DEFAULT 0
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_facts_contact ON mem_facts(contact_user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_facts_status ON mem_facts(status);")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS mem_open_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            source_message_ids TEXT,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'resolved', 'dismissed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_open_items_contact ON mem_open_items(contact_user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_open_items_status ON mem_open_items(status);")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS mem_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_user_id INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            covers_until_message_id INTEGER,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_summaries_contact ON mem_summaries(contact_user_id);")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS mem_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_user_id INTEGER NOT NULL,
            item_type TEXT NOT NULL CHECK(item_type IN ('fact', 'message', 'summary')),
            item_id INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_embeddings_contact ON mem_embeddings(contact_user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mem_embeddings_item ON mem_embeddings(item_type, item_id);")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS mem_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        await db.execute(
            "INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?, ?)",
            (7, "Add Contact Memory schema v7 (mem_contacts, mem_messages, mem_facts, mem_open_items, mem_summaries, mem_embeddings, mem_state)"),
        )

        # Migration version 8: FTS5 Virtual Tables and triggers for Contact Memory
        await db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS mem_messages_fts USING fts5(
            text_redacted,
            content='mem_messages',
            content_rowid='id',
            tokenize='unicode61'
        );
        """)
        await db.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mem_messages_ai AFTER INSERT ON mem_messages BEGIN
            INSERT INTO mem_messages_fts(rowid, text_redacted) VALUES (new.id, new.text_redacted);
        END;
        """)
        await db.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mem_messages_ad AFTER DELETE ON mem_messages BEGIN
            INSERT INTO mem_messages_fts(mem_messages_fts, rowid, text_redacted) VALUES('delete', old.id, old.text_redacted);
        END;
        """)
        await db.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mem_messages_au AFTER UPDATE ON mem_messages BEGIN
            INSERT INTO mem_messages_fts(mem_messages_fts, rowid, text_redacted) VALUES('delete', old.id, old.text_redacted);
            INSERT INTO mem_messages_fts(rowid, text_redacted) VALUES (new.id, new.text_redacted);
        END;
        """)
        await db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS mem_facts_fts USING fts5(
            text,
            content='mem_facts',
            content_rowid='id',
            tokenize='unicode61'
        );
        """)
        await db.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mem_facts_ai AFTER INSERT ON mem_facts BEGIN
            INSERT INTO mem_facts_fts(rowid, text) VALUES (new.id, new.text);
        END;
        """)
        await db.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mem_facts_ad AFTER DELETE ON mem_facts BEGIN
            INSERT INTO mem_facts_fts(mem_facts_fts, rowid, text) VALUES('delete', old.id, old.text);
        END;
        """)
        await db.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mem_facts_au AFTER UPDATE ON mem_facts BEGIN
            INSERT INTO mem_facts_fts(mem_facts_fts, rowid, text) VALUES('delete', old.id, old.text);
            INSERT INTO mem_facts_fts(rowid, text) VALUES (new.id, new.text);
        END;
        """)
        try:
            await db.execute("INSERT INTO mem_messages_fts(mem_messages_fts) VALUES('rebuild');")
            await db.execute("INSERT INTO mem_facts_fts(mem_facts_fts) VALUES('rebuild');")
        except Exception:
            pass

        await db.execute(
            "INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?, ?)",
            (8, "Add Contact Memory FTS5 virtual tables and sync triggers"),
        )

        await db.commit()
