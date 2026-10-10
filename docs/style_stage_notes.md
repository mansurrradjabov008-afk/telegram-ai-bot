# Writing Style System — Stage Notes & Discovery (Phase 0)

## Date & Execution Context
- **Date**: 2026-10-10
- **Branch**: `stage-style`
- **Execution Target**: Checkpoint 1 (Phases 0 through 6)
- **Database Safety Guard**: Active (`tests/test_style_guard.py` blocking any connection to `data/butler.db` during test runs)

---

## 1. Phase 0: Inspection & Discovery Findings

### 1.1 Reply-generator path in `bot/userbot.py`
- **Location**: `bot/userbot.py` lines 464–484 (`_process_debounced_batch`).
- **Flow**:
  1. Batches incoming debounced messages from `_debounce_buffers`.
  2. Retrieves recent conversation history (`db_manager.get_recent_history(chat_id=sender_id, limit=6)`).
  3. Retrieves `memory_context` via `memory_service.get_reply_context(contact_user_id, incoming_text)`.
  4. Calls `ai_engine.generate_reply(incoming_text, sender_name, contact_profile, history, memory_context)`.
- **Pre-Guard & Safety Step**:
  - Step 6 calls `check_sensitive(incoming_text, reply_text, ai_engine)`.
  - Step 7 evaluates `mode == "auto"` and `not is_sensitive`.
  - Step 8 creates `draft` if sensitive or `mode == "draft"`.
- **Integration Points for Style**:
  - Pre-guard honesty step: before auto-send, test if incoming message asks if chatting with AI/bot or owner. If so, route to draft with reason `"Muhim - tasdiq kerak"` (D6).
  - Reply generation hook: fetch computed style guide and inject it into the reply generator prompt context (D1).

### 1.2 Storage of SIZ/SEN, relationship, instructions in `contacts` table
- **Table**: `contacts`
- **Columns**: `user_id`, `username`, `full_name`, `relationship_type`, `custom_instructions`, `language_code`, `created_at`, `updated_at`, `addressing_style` (`siz` | `sen`), `mode` (`auto` | `draft` | `mute`).
- **Current Count**: 11 saved contacts in production.
- **Addressing Style**: `siz` (hurmat / formal) or `sen` (do'stona / informal).

### 1.3 Draft send/edit/reject hooks in `bot/admin_bot.py`
- **Draft resolution handlers**:
  - `draft_send:{id}` -> `_execute_single_draft_send()`: sets draft status to `sent`, sends via userbot, logs to `mem_messages` with `sender="bot_approved"`.
  - `draft_edit:{id}` -> `_execute_draft_edit_and_send()`: sets draft status to `edited_sent`, sends via userbot, logs to `mem_messages` with `sender="owner_edited"`.
  - `draft_reject:{id}` -> `resolve_draft_atomic(id, "rejected")`: rejects draft without sending.
- **Rule D2 Compliance**: Neither `bot_approved` nor `owner_edited` may be used as style sample sources because they originate from bot-generated drafts.

### 1.4 ALL bot-originated send paths and memory attribution
1. **Auto-reply (`bot/userbot.py:541`)**: Logged as `sender="bot_auto"`.
2. **Draft Approved Send (`bot/admin_bot.py:4250`)**: Logged as `sender="bot_approved"`.
3. **Draft Edited Send (`bot/admin_bot.py:526`)**: Logged as `sender="owner_edited"`.
4. **Scheduled Task Send (`bot/userbot.py:620`)**: Logged into `conversation_history` with `role="assistant"` and `sender_name="Men (AI Butler Reja)"`.
5. **Direct Message Send (`bot/userbot.py:620`)**: Logged into `conversation_history` with `role="assistant"`.
6. **Owner Manual Message (`bot/userbot.py:189`)**: Captured when owner types directly from personal Telegram app into private chat with saved contact -> Logged as `sender="owner_manual"`.

### 1.5 Message counts in production DB backup
- `mem_messages`:
  - `contact`: 47
  - `bot_auto`: 22
  - `owner_manual`: 8
  - `bot_approved`: 3
  - `owner_edited`: 2
- Total `owner_manual` in live DB: 8 rows (Uzbek Latin).
- `conversation_history`:
  - `assistant`: 76
  - `contact`: 89

### 1.6 `BOT_CUTOFF` Determination (Evidence-Based)
Earliest bot-originated message timestamps across all 4 tables:
- `scheduled_tasks` (status = 'executed'): `2026-10-08 05:26:14`
- `conversation_history` (role IN ('assistant', 'model')): `2026-10-08 05:28:04`
- `drafts` (status IN ('sent', 'edited_sent')): `2026-10-08 17:50:21`
- `mem_messages` (sender IN ('bot_auto', 'bot_approved', 'owner_edited')): `2026-10-10 05:07:00`
- **Earliest Bot Timestamp (`BOT_CUTOFF`)**: `2026-10-08 05:26:14`
- **Import Cutoff (`BOT_CUTOFF - STYLE_CUTOFF_MARGIN_HOURS=24h`)**: `2026-10-07 05:26:14`
- Any messages older than `2026-10-07 05:26:14` are proven to be 100% human-authored by the owner.

### 1.7 Check for `/memory import`
- `/memory import` command does **NOT** exist in the codebase.
- No history import command was previously implemented.

### 1.8 Telethon `iter_messages` behavior
- Calling `client.iter_messages()` in Telethon issues `messages.getHistory` MTProto requests.
- MTProto never marks messages as read during history retrieval.
- Marking messages as read only occurs if `client.send_read_acknowledge()` is explicitly invoked.
- `send_read_acknowledge` is verified to be absent in the codebase.

### 1.9 Sensitive Guard Pure Keyword Function
- `services/sensitive_guard.py` exports `check_sensitive_keywords(text: str) -> List[str]`.
- Reads `SENSITIVE_CATEGORIES` (covering Uzbek Latin, Uzbek Cyrillic, Russian, English) and regexes for cards/amounts.
- Completely pure function (no async, no external network, no AI calls).
- Ideal for D5 few-shot example screening.

### 1.10 Entity & Name Sources for D4 Leakage Guard
- Contacts names and usernames: `contacts.full_name`, `contacts.username`.
- Facts entities: `mem_facts.text` (categories `identity`, `preference`, `interest`, etc.).
- D4 token filter ensures no frequent phrase contains tokens equal to contact names, usernames, or mem_facts entities.
