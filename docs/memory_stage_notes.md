# Contact Memory - Stage Notes & Architecture Ledger

Last Updated: 2026-10-09 (Phase 0 Complete)

## 1. Verified Facts About the Codebase (Phase 0 Proof)
- **Branch**: `stage-memory`
- **Database File**: `data/butler.db` (Backup: `data/butler_backup_20261009_184522.db`)
- **SQLite Engine**: SQLite version `3.45.1`
- **Journal Mode**: `wal` (Write-Ahead Logging enabled with `busy_timeout=5000`)
- **SQLite FTS5**: Fully available and verified (`CREATE VIRTUAL TABLE USING fts5` succeeded)
- **Current DB Row Counts**:
  - `contacts`: 5 contacts (identified primarily by `user_id INTEGER PRIMARY KEY`)
  - `conversation_history`: 76 messages
  - `drafts`: 3 rows (unredacted legacy text per H13)
  - `scheduled_tasks`: 6 rows
  - `digest_sources`: 3 sources
  - `digest_posts`: 71 posts
  - `digest_runs`: 1 run
  - `schema_versions`: 5 migrations applied (v1, v3, v4, v5, v6)
- **AI SDK**: `google-genai` (version 1.62.0), `NEW_SDK = True`.
- **Embedding Model Verification**:
  - `text-embedding-004` is NOT supported for v1beta under `google-genai` (returns HTTP 404).
  - Officially supported embedding models from `client.models.list()`: `models/gemini-embedding-001`, `models/gemini-embedding-2`, `models/gemini-embedding-2-preview`.
  - Minimal live embedding verification with `gemini-embedding-001` SUCCEEDED (3072 dimensions).
- **Telethon Client**: Version `1.45.0`.
  - `events.MessageEdited` is exposed by Telethon (`hasattr(events, 'MessageEdited') == True`).
  - Current codebase only listens to `events.NewMessage(incoming=True, func=lambda e: e.is_private)`.
  - No active listener for `outgoing=True` exists yet; owner manual messages are polled on demand via `client.get_messages(peer, limit=10)`.
- **Existing Reply Integration Point**:
  - `bot/userbot.py` line 333: `reply_text = await self.ai_engine.generate_reply(incoming_text=combined_text, sender_name=first_name, contact_profile=contact, history=history)`.
  - Current context passed: `history` (last 6 raw messages from `conversation_history`).
- **Sender Attribution & Draft Flows (H8)**:
  - `bot_auto`: Sent automatically from userbot (`_send_with_floodwait`).
  - `bot_approved`: Sent via callback `draft_send:{id}` unchanged.
  - `owner_edited`: Sent via callback `draft_edit:{id}` with owner-supplied text.
  - `owner_manual`: Sent directly from Telegram account by owner.
  - `contact`: Incoming private message from saved contact.
- **Contact Deletion Hook (Point 3 / H10)**:
  - In `bot/admin_bot.py:handle_crm_delete`: currently deletes via `db_manager.delete_contact(contact.user_id)`.
- **Normalization Utilities**:
  - `services/digest_cleaner.py` provides `transliterate_uzbek_cyrillic_to_latin` and string normalization.
- **Owner Identification & Config**:
  - `config.py`: `settings.owner_id_effective` (numeric Telegram user ID).
- **Test Suite Baseline (P7)**:
  - 123/123 tests passing (100% pass rate).

---

## 2. Phase Status
- [x] **Phase 0: Inspection & Foundation Plan** — COMPLETE (Waiting for user "OK")
- [ ] **Phase 1: Storage, Redaction, Logging** — PENDING
- [ ] **Phase 2: Retrieval Foundation** — PENDING
- [ ] **Phase 3: Fact Extraction (code-verified)** — PENDING
- [ ] **Phase 4: Rolling Summaries** — PENDING
- [ ] **Phase 5: Context Assembly & Reply Integration** — PENDING
- [ ] **Phase 6: Owner Commands & Deletion Hook** — PENDING
- [ ] **Phase 7: Comprehensive Test Suite** — PENDING
- [ ] **Phase 8: Final Report & Shadow Check** — PENDING

---

## 3. Architecture Decisions & Planned Config Keys
- **Default Embedding Model**: `gemini-embedding-001` (proven live in Phase 0 inspection).
- **Embedding Fallback**: SQLite FTS5 full-text keyword search + recency weighting when embeddings are offline or failing.
- **Database Write Serialization**: Dedicated `asyncio.Lock` / queue for memory tables to prevent lock contention with userbot and digest services.
- **Config Keys to be Added in `config.py`**:
  - `MEMORY_ENABLED`: bool = True (feature flag; if false, reply pipeline behaves exactly as before)
  - `MEMORY_LOG_MUTED`: bool = False (H3: muted contacts store nothing)
  - `MEM_MESSAGE_RETENTION_DAYS`: int = 180 (retention window for raw memory messages)
  - `MEM_EMBEDDING_MODEL`: str = "gemini-embedding-001"
  - `MEM_EMBEDDING_DIM`: int = 3072
  - `MEM_MAX_EMBED_CALLS_PER_DAY`: int = 200
  - `MEM_EXTRACT_IDLE_MINUTES`: int = 5
  - `MEM_MAX_EXTRACT_CALLS_PER_DAY`: int = 50
  - `MEM_MAX_MESSAGES_PER_BATCH`: int = 15
  - `MEM_MAX_FACT_LENGTH`: int = 150
  - `MEM_FACT_VALIDITY_DAYS`: int = 30
  - `MEM_CONTEXT_TIMEOUT_SECONDS`: float = 1.0
  - `MEM_MAX_CONTEXT_TOKENS`: int = 1200
  - `MEM_IMPORT_ENABLED`: bool = False
  - `MEM_IMPORT_MAX_MESSAGES`: int = 200

---

## 4. Open Questions & Assumptions
- **Assumption 1**: Existing `conversation_history` table will remain intact for backwards compatibility; new memory messages will be stored in `mem_messages` with strict redaction and sender labeling.
- **Assumption 2**: Telegram message edits (`events.MessageEdited`) will only update `text_redacted` in `mem_messages` if the message was already captured, re-applying full redaction and setting `edited_at = CURRENT_TIMESTAMP`.
- **Known Limitation (H13)**: Existing `drafts` table holds unredacted text from prior development phases. The Contact Memory system will never read from `drafts` for retrieval or extraction. On `/forget @user`, the contact's text in `drafts` will be blanked out.
