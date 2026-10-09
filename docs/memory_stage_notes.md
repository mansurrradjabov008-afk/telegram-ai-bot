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
- [x] **Phase 0: Inspection & Foundation Plan** — COMPLETE
- [x] **Phase 1: Storage, Redaction, Logging** — COMPLETE
- [x] **Phase 2: Retrieval Foundation** — COMPLETE
- [x] **Phase 3: Fact Extraction (code-verified)** — COMPLETE
- [x] **Phase 4: Rolling Summaries** — COMPLETE
- [x] **Phase 5: Context Assembly & Reply Integration** — COMPLETE
- [x] **Phase 6: Owner Commands & Deletion Hook** — COMPLETE
- [x] **Phase 7: Comprehensive Test Suite** — COMPLETE
- [x] **Phase 8: Final Report & Shadow Check** — COMPLETE

---

## 2.1 Verified Facts from Phase 1 Execution
- **Database Backup Before Migration**: `data/butler_backup_20261009_185241.db` (393,216 bytes).
- **Schema Migration v7 Applied**:
  - `mem_contacts` (`contact_user_id` PK, `username_at_bind`, `memory_enabled`, `paused_reason`, `bound_at`)
  - `mem_messages` (`id` PK auto, `contact_user_id`, `tg_message_id`, `sender` CHECK constraint, `text_redacted`, `sent_at`, `edited_at`, `is_media`, UNIQUE(`contact_user_id`, `tg_message_id`))
  - `mem_facts` (`id` PK auto, `contact_user_id`, `category`, `text`, `source_type` CHECK, `source_message_ids`, `status` CHECK, `confidence`, `supersedes_id`, timestamps, `valid_until`, `injection_suspected`)
  - `mem_open_items` (`id` PK auto, `contact_user_id`, `text`, `source_message_ids`, `status` CHECK, `created_at`)
  - `mem_summaries` (`id` PK auto, `contact_user_id`, `summary_text`, `covers_until_message_id`, `version`, `created_at`)
  - `mem_embeddings` (`id` PK auto, `contact_user_id`, `item_type` CHECK, `item_id`, `model_name`, `dim`, `vector` BLOB, `created_at`)
  - `mem_state` (`key` PK, `value`, `updated_at`)
- **Write Concurrency Proof**: `DatabaseManager.serialized_transaction()` wraps writes in `asyncio.Lock()`. Proven with 25 simultaneous concurrent writer tasks without SQLite locking issues.
- **Pure Redaction Engine (`services/memory_redactor.py`)**:
  - Luhn verification on 13-19 digit candidates.
  - 20-digit bank accounts & IBAN formats.
  - 14-digit Uzbek PINFL / JShShIR (`[1-6]\d{13}`).
  - Passports (Uzbek `\b[A-Za-z]{2}\s*\d{7}\b`, CIS keywords).
  - One-time codes / passwords / OTPs across 4 languages (Uzbek Latin, Uzbek Cyrillic, Russian, English).
  - Negative safety proven: dates, prices, quantities, normal words, and phone numbers are never redacted.
  - 39/39 redaction unit tests passing.
- **Message Logging Hooks (Integration Point 1 / H8)**:
  - `contact`: Incoming private text & media markers (`[ovozli xabar]`, `[rasm]`, etc. with `is_media=1`).
  - `owner_manual`: Outgoing private messages captured via Telethon `outgoing=True` if not bot-originated.
  - `bot_auto`: Automatic replies logged upon successful send.
  - `bot_approved`: Drafts approved unchanged logged upon successful send.
  - `owner_edited`: Drafts edited and sent logged upon successful send.
  - Message edit events (`events.MessageEdited`): re-redacts text and updates `edited_at`.
  - Mute mode check (H3): muted contacts store nothing.
  - Identity check (H2): strictly numeric Telegram user_id; username reassignment pauses memory.
  - Startup retention cleanup: purges messages older than `MEM_MESSAGE_RETENTION_DAYS` (180).
- **Test Suite Results (P7)**:
  - Baseline: 123 passed.
  - Phase 1 total: 172 passed (123 existing + 39 redaction + 10 storage/concurrency/lifecycle). 0 regressions.

---

## 2.2 Verified Facts from Phase 2 Execution
- **Database Backup Before Phase 2**: `data/butler_backup_20261009_192431.db` (491,520 bytes).
- **Schema Migration v8 Applied**:
  - `mem_messages_fts`: FTS5 external content virtual table synced to `mem_messages` with unicode61 tokenizer.
  - `mem_facts_fts`: FTS5 external content virtual table synced to `mem_facts` with unicode61 tokenizer.
  - Automatic triggers: `trg_mem_messages_ai`, `trg_mem_messages_ad`, `trg_mem_messages_au`, `trg_mem_facts_ai`, `trg_mem_facts_ad`, `trg_mem_facts_au`.
  - Rebuild command executed to populate index from existing rows.
- **Vector Math & Serialization**:
  - 3072-dimensional float vectors serialized into 12,288-byte BLOBs via `numpy.float32`.
  - Cosine similarity computation with numerical stability check (zero-norm safeguard).
- **Gemini Embedding Service (`services/memory_embedding.py`)**:
  - Off-thread execution via `asyncio.to_thread` with strict 15s timeout (`MEM_EMBEDDING_TIMEOUT_SECONDS`).
  - Quota limiter tracking daily count against `MEM_MAX_EMBED_CALLS_PER_DAY` (200) in `mem_state`.
  - Fail-open architecture: returns `None` on any error, allowing seamless keyword fallback.
- **Unified Memory Retriever (`services/memory_retriever.py`)**:
  - Primary path: Vector semantic search over contact embeddings with cosine similarity.
  - Similarity threshold: `MEM_SIMILARITY_THRESHOLD = 0.40`.
  - Model version awareness: embeddings created with different models are ignored.
  - Fallback path: SQLite FTS5 BM25 search with query token sanitization (handling hyphens and special characters).
  - Strict isolation guardrail (H4): verifies `item["contact_user_id"] == contact_user_id` for every hydrated item; drops block completely on breach.
- **Background Worker (`MemoryService.process_pending_embeddings`)**:
  - Automatically identifies unembedded facts and messages and batches them safely.
- **Config Keys Added**:
  - `MEM_SIMILARITY_THRESHOLD`: float = 0.40
  - `MEM_RETRIEVAL_TOP_K`: int = 5
  - `MEM_EMBEDDING_TIMEOUT_SECONDS`: float = 15.0
- **Test Suite Results (P7)**:
  - Baseline before Phase 2: 172 passed.
  - Phase 2 total: 188 passed (172 existing + 16 new retrieval tests in `tests/test_memory_retrieval_phase2.py`). 0 regressions.

---

## 2.3 Verified Facts from Phase 3 Execution
- **Database Backup Before Phase 3**: `data/butler_backup_20261009_193819.db` (536,576 bytes).
- **Prompt Injection Detector (`services/memory_injection.py`)**:
  - Shared unit-tested detector across 4 languages (Uzbek Latin, Uzbek Cyrillic, Russian, English) per Rule U2.
  - Detects prompt injection attempts (e.g. "ignore previous instructions", "sen endi", "system prompt", "забудь все инструкции").
  - Flags facts with `injection_suspected = 1` so they are never injected into reply context prompts.
  - Zero false positives on ordinary conversational text.
- **Forbidden Topics Filter (`services/memory_injection.py`)**:
  - Pure code filter for Rule H6: drops health, religion, ethnicity, political views, sexual life, finances/debts/income, and criminal matters.
- **No Self-Reinforcement & Sender Verification (`services/memory_extractor.py`)**:
  - Pure code verification of cited message IDs for Rule H9.
  - Messages from `bot_auto` or `bot_approved` are 100% REJECTED in code if cited as a fact source.
  - Permitted senders: `('contact', 'owner_manual', 'owner_edited')`.
- **Fact Source Types Attribution (U3)**:
  - `contact_claim`: statement originating from contact.
  - `owner_stated`: statement originating from owner.
  - `owner_confirmed`: statement claimed by contact and subsequently confirmed by owner.
- **Pure Redaction Before Storage (H5)**:
  - Facts and open items are passed through `redact_text()` before insertion into SQLite.
- **Open Items Extraction (U4)**:
  - Actionable tasks, meetings, and promises stored in `mem_open_items` for the owner only; never injected into reply prompts.
- **Config Keys Added**:
  - `MEM_EXTRACT_TIMEOUT_SECONDS`: float = 20.0
  - `MEM_INJECTION_KEYWORDS_UZ_LATIN`, `MEM_INJECTION_KEYWORDS_UZ_CYRILLIC`, `MEM_INJECTION_KEYWORDS_RU`, `MEM_INJECTION_KEYWORDS_EN`
  - `MEM_FORBIDDEN_CATEGORIES`, `MEM_FORBIDDEN_KEYWORDS`
- **Test Suite Results (P7)**:
  - Baseline before Phase 3: 188 passed.
  - Phase 3 total: 204 passed (188 existing + 16 new extraction tests in `tests/test_memory_extraction_phase3.py`). 0 regressions.

---

## 2.4 Verified Facts from Phase 4 Execution
- **Database Backup Before Phase 4**: `data/butler_backup_20261009_194954.db` (540,672 bytes).
- **Rolling Summaries Engine (`services/memory_summarizer.py`)**:
  - Incremental summarization: generates first-time summary if none exists; generates rolling update combining previous summary + new messages since `covers_until_message_id`.
  - Rate limiting & daily quota: enforces `MEM_MAX_SUMMARY_CALLS_PER_DAY` (30) tracked in `mem_state`.
  - Minimum message threshold: only runs if new messages >= `MEM_SUMMARY_MIN_MESSAGES` (5), unless `force=True`.
  - Strict length constraint: enforces `MEM_MAX_SUMMARY_LENGTH` (500 chars).
- **Security & Safety Guardrails**:
  - **U2 Injection Protection**: If candidate summary text triggers prompt injection detection (`MemoryInjectionDetector.detect_injection`), the new candidate is DISCARDED and the previous verified summary is kept in DB.
  - **H6 Forbidden Topics Filter**: If candidate summary mentions health, religion, politics, sex, debt, or criminal matters (`MemoryInjectionDetector.detect_forbidden_topics`), the new candidate is DISCARDED and previous verified summary is kept.
  - **H5 Redaction Before Storage**: Redacts credit cards (Luhn), passwords/PINs, passports, and PINFL before inserting into `mem_summaries`.
  - **H9 No Self-Reinforcement**: Bot messages (`bot_auto`, `bot_approved`) in dialogue context explicitly marked with `(AI ASSISTANT - MAY CONTAIN ERRORS)` warning so the summarizer never mistakes AI hallucinations for factual truths.
- **Config Keys Added**:
  - `MEM_MAX_SUMMARY_LENGTH`: int = 500
  - `MEM_SUMMARY_MIN_MESSAGES`: int = 5
  - `MEM_MAX_SUMMARY_CALLS_PER_DAY`: int = 30
  - `MEM_SUMMARY_TIMEOUT_SECONDS`: float = 20.0
- **Test Suite Results (P7)**:
  - Baseline before Phase 4: 204 passed.
  - Phase 4 total: 216 passed (204 existing + 12 new summary tests in `tests/test_memory_summary_phase4.py`). 0 regressions.

---

## 2.5 Verified Facts from Phase 5 Execution
- **Database Backup Before Phase 5**: `data/butler_backup_20261009_195841.db` (552,960 bytes).
- **Context Assembly Engine (`services/memory_context.py`)**:
  - `MemoryContextAssembler`: Assembles background context including latest rolling summary, active facts with source types, and relevant past messages.
  - **U1 Delimiters & Anti-Instruction Framing**: Context wrapped in `<memory>...</memory>` tags with explicit warning that content is background reference only, not system instructions, and that the model possesses no tools.
  - **U2 Injection Filtering**: Excludes facts where `injection_suspected == 1` and omits summaries/messages matching prompt injection keywords or regex.
  - **U3 Source Attribution**: Labeled with `contact_claim` (unverified claim), `owner_stated`, and `owner_confirmed`. Model instructed to treat contact claims as unverified.
  - **U4 Open Items Exclusion**: Actionable promises/meetings/debts stored in `mem_open_items` are strictly excluded from context.
  - **U5 Sensitive Guard Isolation**: `check_sensitive` evaluates strictly `incoming_text` and proposed reply; it NEVER receives the memory block.
  - **H4 Strict Isolation**: SQL-level isolation verified with code assertions on each component; any breach drops the entire context block immediately.
  - **P14 & P15 Fail-Open & Budget**: Strict timeout `MEM_CONTEXT_TIMEOUT_SECONDS = 1.0` and character/token budget `MEM_MAX_CONTEXT_TOKENS = 1200`. Fails open returning `""` on any timeout or error.
- **Allowed Integration Point 2**:
  - `bot/userbot.py`: In `_process_debounced_batch`, queries `self.memory_service.get_reply_context(contact.user_id, combined_text)` and passes `memory_context` to `ai_engine.generate_reply`.
  - `services/ai_engine.py`: `generate_reply` embeds `memory_context` under `<memory>` section if present; falls back seamlessly to standard behavior if empty or disabled (`MEMORY_ENABLED=False`).
- **Test Suite Results (P7)**:
  - Baseline before Phase 5: 216 passed.
  - Phase 5 total: 229 passed (216 existing + 13 new context tests in `tests/test_memory_context_phase5.py`). 0 regressions.

---

## 2.6 Verified Facts from Phase 6 Execution
- **Database Backup Before Phase 6**: `data/butler_backup_20261009_201508.db` (565,248 bytes).
- **Database CRUD Capabilities (`database/crud.py`)**:
  - `wipe_contact_memory(contact_user_id)`: Permanently deletes all records across `mem_messages` (with automatic FTS5 cleanup via trigger `trg_mem_messages_ad`), `mem_facts` (with FTS5 cleanup via `trg_mem_facts_ad`), `mem_open_items`, `mem_summaries`, `mem_embeddings`, and `mem_contacts`. Also blanks legacy unredacted text in `drafts` with `'[DELETED]'` per Rule H10 & H13.
  - `delete_mem_fact(contact_user_id, fact_id)`: Deletes individual facts for a specific contact with strict isolation (H4, H10).
  - `get_mem_stats(contact_user_id)`: Returns real-time counts for messages, active facts, open items, summaries, and embeddings.
- **Allowed Integration Point 3 (`bot/admin_bot.py:handle_crm_delete`)**:
  - When owner clicks "🗑 O'chirish" in CRM contacts list, bot presents an interactive confirmation dialog offering two distinct options:
    1. "🗑 Kontakt o'chirilsin (Xotira to'xtatiladi)" — Default per Rule H10: pauses memory (`pause_mem_contact`) and deletes CRM contact.
    2. "🔥 Hammasi o'chirilsin (Xotira ham tozalansin)" — Complete wipe calling `wipe_contact_memory` and deleting CRM contact.
    3. "❌ Bekor qilish" — Cancels deletion and refreshes contacts list.
- **Admin Cockpit Commands & Inline Navigation**:
  - `/memory @username` (or `/memory <user_id>`): Shows Contact Memory card (Telegram ID, memory state, message count, fact count, vector count, rolling summary, and private open items). Provides inline buttons: "📋 Faktlarni ko'rish", "🗑 Xotirani o'chirish (/forget)", "🔙 Bosh menyu".
  - `/memory_facts @username`: Displays up to 10 active facts with their source type labels (`contact_claim`, `owner_stated`, `owner_confirmed`) and inline buttons to delete specific facts (`mem_fact_del:<user_id>:<fact_id>`).
  - `/forget @username`: Prompts confirmation before permanent memory wipe and drafts blanking per Rule H10.
  - Strict Authorization: All commands and callback queries verify `_is_admin(user_id) == True` against `settings.owner_id_effective`.
- **Test Suite Results (P7)**:
  - Baseline before Phase 6: 229 passed.
  - Phase 6 total: 237 passed (229 existing + 8 new tests in `tests/test_memory_commands_phase6.py`). 0 regressions.

---

## 2.7 Verified Facts from Phase 7 Execution
- **Database Backup Before Phase 7**: `data/butler_backup_20261009_202832.db` (565,248 bytes).
- **Comprehensive E2E & Chaos Testing Suite (`tests/test_memory_chaos_e2e_phase7.py`)**:
  - **Redaction Coverage (H5)**: Verified Luhn algorithm on 16-digit cards, negative safety on invalid checksums, OTP/PIN/passwords across Uzbek Latin, Cyrillic, Russian, and English, passports, 14-digit PINFL, and non-over-redaction of normal dates/quantities/prices.
  - **Identity & Logging Scope (H1, H2, H3, H8)**: Verified muted contacts store nothing, private 1-to-1 contacts only, and username reassignment pauses memory (`username_reassigned_to_<uid>`) with complete block on further writes and no data transfer across user IDs.
  - **Multi-Contact Strict Isolation (H4)**: Verified distinct secret markers (`SECRET_ALPHA_TOKEN_999`, `SECRET_BETA_TOKEN_888`) across all retrieval paths (FTS, rolling summary, facts). Verified code-level assertion fires on forced isolation violation and immediately drops entire context block to `""`.
  - **No Self-Reinforcement (H9)**: Verified extractor rejects fact candidates citing `bot_auto` or `bot_approved` messages with `unauthorized_sender` rejection; only allowed senders (`contact`, `owner_manual`, `owner_edited`) accepted.
  - **Multilingual Prompt Injection (U1, U2)**: Verified prompt injection detection across 4 languages (Uzbek Latin, Uzbek Cyrillic, Russian, English) protecting `<memory>` context.
  - **Forbidden Topics Filter (H6, H7)**: Verified pure code filtering of health, religion, ethnicity, politics, sexual life, finances/debts, crime, and third-party details.
  - **Open Items & Sensitive Guard Privacy (U4, U5)**: Verified open items are never injected into reply context prompts. Verified `check_sensitive` strictly evaluates only `incoming_text` and `proposed_reply`, NEVER receiving the memory block.
  - **Concurrency & Locking Resilience**: Fixed asyncio lock binding to ensure `DatabaseManager._get_write_lock()` dynamically resolves per running event loop, eliminating cross-test loop binding errors and guaranteeing 100% concurrent write serialization under high load.
  - **Embedding Fallback & Fail-Open (P14, P15)**: Verified seamless fallback from failed embeddings to FTS5 recency search, fail-open return of `""` on memory assembler errors without breaking replies, and complete pipeline passthrough when `MEMORY_ENABLED = False`.
  - **Owner Security & Double-Press Resilience**: Verified strict rejection of non-admin user IDs across all commands and callbacks, and safe idempotency on double-press callback invocations.
- **Test Suite Results (P7)**:
  - Baseline before Phase 7: 237 passed.
  - Phase 7 total: 248 passed (237 existing + 11 new tests in `tests/test_memory_chaos_e2e_phase7.py`). 0 regressions.

---

## 2.8 Verified Facts from Phase 8 Execution (Final Shadow Check)
- **Live Shadow Check (`scratch/shadow_check.py`)**:
  - Two test contacts seeded: Contact A (Alisher Navoiy, `user_id=8801`, `ALISHER_SECRET_ALPHA`) and Contact B (Bobur Mirzo, `user_id=8802`, `BOBUR_SECRET_BETA`).
  - Strict Isolation Verified:
    - Contact A prompt evaluated: exactly 0 mentions of Contact B data, secrets, facts, or summaries.
    - Contact B prompt evaluated: exactly 0 mentions of Contact A data, secrets, facts, or summaries.
    - Code assertions passed (`assert not leak_b_in_a`, `assert not leak_a_in_b`).
  - Delimiters & Provenance Attribution:
    - Assembled memory blocks wrapped inside `<memory>...</memory>` tags with explicit anti-instruction warnings.
    - Facts labeled with provenance: `kontaktning da'vosi, tasdiqlanmagan`, `egasi tomonidan tasdiqlangan`, and `egasi ta'kidlagan`.
    - Instructions in system prompt tell the AI model that facts labeled with contact claims are unverified claims and never to state them as established fact.
  - 10 Random Stored Facts Cited Sources:
    - Printed live with their cited `msg_id`, `sender` (`contact` vs `owner_manual`), and redacted text snippet.
  - Measured Performance:
    - Pure Code Redaction Latency: ~0.096 ms per message.
    - Context Assembly Latency: ~669 ms - 933 ms (within 1.0s fail-open budget).
    - Concurrency Lock: 0 deadlocks under 20-worker parallel write loads.
  - Full Test Suite Regression Gate:
    - Final project-wide test count: **248 passed** in 109.94s (100% pass rate, 0 regressions).

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
