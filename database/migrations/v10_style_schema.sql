-- Migration v10: Writing Style System Schema
-- Adds style_samples, style_notes, and style_profiles tables

CREATE TABLE IF NOT EXISTS style_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_user_id INTEGER,
    tg_message_id INTEGER,
    text_redacted TEXT NOT NULL,
    char_len INTEGER NOT NULL,
    token_len INTEGER NOT NULL,
    lang TEXT NOT NULL,
    register TEXT,
    sent_at TIMESTAMP NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('import', 'live')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_style_samples_contact ON style_samples(contact_user_id);
CREATE INDEX IF NOT EXISTS idx_style_samples_lang ON style_samples(lang);
CREATE INDEX IF NOT EXISTS idx_style_samples_register ON style_samples(register);
CREATE INDEX IF NOT EXISTS idx_style_samples_sent_at ON style_samples(sent_at);
CREATE INDEX IF NOT EXISTS idx_style_samples_source ON style_samples(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_style_samples_contact_tg_msg 
ON style_samples(contact_user_id, tg_message_id) 
WHERE contact_user_id IS NOT NULL AND tg_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS style_notes (
    scope TEXT PRIMARY KEY,
    note_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS style_profiles (
    scope TEXT NOT NULL,
    lang TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    phrases_json TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sample_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, lang)
);

INSERT OR IGNORE INTO schema_versions (version, description)
VALUES (10, 'Add Writing Style schema (style_samples, style_notes, style_profiles)');
