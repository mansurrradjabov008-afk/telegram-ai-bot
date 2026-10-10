-- Migration v9: Enforce mem_facts category enum via SQLite Triggers
-- Prepared for Maintenance Window (Do NOT apply outside maintenance)
-- Supported Enum: ('identity', 'preference', 'interest', 'important_date', 'relationship_note', 'ongoing_topic', 'other')

CREATE TRIGGER IF NOT EXISTS trg_mem_facts_category_insert
BEFORE INSERT ON mem_facts
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.category NOT IN ('identity', 'preference', 'interest', 'important_date', 'relationship_note', 'ongoing_topic', 'other')
        THEN RAISE(ABORT, 'INVALID_CATEGORY: category must be one of identity, preference, interest, important_date, relationship_note, ongoing_topic, other')
    END;
END;

CREATE TRIGGER IF NOT EXISTS trg_mem_facts_category_update
BEFORE UPDATE OF category ON mem_facts
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.category NOT IN ('identity', 'preference', 'interest', 'important_date', 'relationship_note', 'ongoing_topic', 'other')
        THEN RAISE(ABORT, 'INVALID_CATEGORY: category must be one of identity, preference, interest, important_date, relationship_note, ongoing_topic, other')
    END;
END;
