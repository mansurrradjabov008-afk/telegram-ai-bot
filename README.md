# JARVIS — Executive AI Telegram Butler & Personal Automation Agent

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Telethon MTProto](https://img.shields.io/badge/Telethon-MTProto-orange.svg)](https://docs.telethon.dev/)
[![aiogram 3.x](https://img.shields.io/badge/aiogram-3.x-2ba1ff.svg)](https://docs.aiogram.dev/)
[![Google Gemini Flash](https://img.shields.io/badge/Gemini-Flash%20LLM-4E75F8.svg)](https://ai.google.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An enterprise-grade, asynchronous AI agent that acts as an autonomous digital twin and executive butler on Telegram. Built on **Telethon MTProto Client**, **aiogram 3.x Admin Cockpit**, **Google Gemini 2.5 Flash LLM**, and **SQLite WAL** persistence.

---

## 🏛 Architecture Overview

```mermaid
flowchart TD
    User([Telegram User / Contacts]) <-->|MTProto| Userbot[Telethon Userbot Client]
    Admin([Owner / Administrator]) <-->|Bot API| Cockpit[aiogram 3.x Admin Cockpit]
    
    subgraph Core Engine [Asynchronous Runtime Engine]
        Userbot --> Dispatcher[Event Ingestion & Debounce Queue]
        Dispatcher --> RateLimiter[Token Bucket Rate Limiter]
        RateLimiter --> SafetyGuard[Two-Stage Guardrail & Intent Classifier]
        
        SafetyGuard -->|Routine Inquiry| AIEngine[Gemini Flash LLM Engine]
        SafetyGuard -->|Financial / Critical Action| Escalation[Escalation to Cockpit]
        Escalation --> Cockpit
        
        AIEngine --> HumanDelay[Human Keystroke Simulator & Delay Engine]
        HumanDelay --> Userbot
    end

    subgraph Persistence [State & Reliability]
        DB[(SQLite WAL Persistence)]
        Watchdog[Self-Healing Watchdog Supervisor]
        Scheduler[Distributed Async Scheduler]
    end

    SafetyGuard <--> DB
    AIEngine <--> DB
    Scheduler <--> DB
    Watchdog -.->|Heartbeat Ping / Memory Recycling| Core Engine
```

---

## 🚀 Key Architectural Capabilities

### 1. Dual-Client Architecture (MTProto + Bot API)
- **Userbot Client (`Telethon`):** Operates seamlessly on personal Telegram MTProto protocol, executing natural-language dialogue, smart auto-reply, and draft management.
- **Admin Cockpit (`aiogram 3.x`):** Dedicated executive control panel for the bot owner with interactive inline keyboards, real-time audit approvals, and CRM controls.

### 2. Autonomous Two-Stage Guardrail & Escalation System
- Routine conversational inquiries are resolved autonomously within configured conversational guidelines.
- High-stakes topics (financial transfers, business commitments, calendar bookings, authentication codes) trigger an immediate **Approval Request Draft** sent to the owner's Admin Cockpit.
- No hallucinated promises or unauthorized commitments can be dispatched without explicit owner confirmation.

### 3. Contact Relationship CRM (Persistent Context Memory)
- Per-contact memory profiles: store tone guidelines, formal vs. informal registers, and custom context.
- Conversation history tracking with token-optimized context window summarization.

### 4. Human Behavior & Anti-Ban Protection
- Variable typing action intervals (`HUMAN_DELAY_MIN` to `HUMAN_DELAY_MAX`).
- Configurable debounce windows (coalesces rapid multi-message bursts into a single coherent prompt).
- Strict rate limiting adhering to Telegram's flood-wait algorithms.

### 5. 24/7 Self-Healing & Supervisory Daemon
- Continuous health ping check and process watchdog supervisor.
- Automatic SQLite WAL checkpointing and database file size recycling.
- Graceful restart recovery on network interruption.

---

## 📂 Repository Structure

```
telegram_ai_butler/
├── bot/
│   ├── admin_bot.py           # aiogram 3.x Admin Cockpit (buttons, alerts, approval actions)
│   └── userbot.py             # Telethon MTProto client (message dispatch, typing action)
├── config.py                  # Pydantic Settings configuration & env validation
├── database/
│   ├── connection.py          # Asynchronous aiosqlite connection pool & WAL manager
│   ├── crud.py                # CRUD layer for CRM, scheduled tasks, and chat history
│   └── models.py              # Schema models for contacts, logs, and drafts
├── scripts/
│   └── watchdog_supervisor.py # Daemon supervisor for health checks & memory recycling
├── services/
│   ├── ai_engine.py           # Google Gemini 2.5 Flash inference & safety filters
│   ├── digest_classifier.py   # NLP spam filtering & relevance scoring
│   ├── digest_service.py      # Channel and group intelligence monitor
│   ├── group_radar.py         # Keyword radar and incident detector
│   ├── rate_limiter.py        # Token-bucket rate limiter with flood control
│   └── scheduler_service.py   # Async task scheduler for planned dispatches
├── tests/
│   └── test_conversational_richness.py # Automated test suite for AI response quality
├── main.py                    # Unified entrypoint (lifespan coordinator)
├── requirements.txt           # Dependency specifications
└── .env.example               # Environment variables template
```

---

## 🛠 Tech Stack

- **Language:** Python 3.11+
- **Telegram Frameworks:** Telethon (MTProto), aiogram 3.x
- **LLM Provider:** Google Gemini Flash API (`google-generativeai`)
- **Database:** SQLite with Write-Ahead Logging (`aiosqlite`)
- **Validation:** Pydantic 2.x
- **Concurrency:** Asyncio, uvloop (Linux), threading supervisors

---

## ⚡ Quick Start

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/mansurrradjabov008-afk/telegram-ai-bot.git
cd telegram-ai-bot
python -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows:
venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment

Copy `.env.example` to `.env`:

```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE_NUMBER=+99890XXXXXXX
ADMIN_BOT_TOKEN=your_botfather_token
ADMIN_USER_ID=your_telegram_id
GEMINI_API_KEY=your_gemini_api_key
```

### 4. Run the Engine

```bash
python main.py
```

---

## 🛡 Security & Privacy

- Sensitive credentials (`.env`, `*.session`, database files) are strictly excluded from source control.
- All outbound messages passing through the LLM undergo local regex-based PII sanitization.

---

## 👤 Author

**Mansurjon Radjabov**
- LinkedIn: [linkedin.com/in/mansurjon-radjabov](https://linkedin.com/in/mansurjon-radjabov)
- Telegram: [@radjabovmansur](https://t.me/radjabovmansur)
- GitHub: [@mansurrradjabov008-afk](https://github.com/mansurrradjabov008-afk)
