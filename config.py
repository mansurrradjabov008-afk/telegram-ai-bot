"""
Configuration module for Antigravity Telegram AI Executive Butler.
Uses Pydantic Settings for strict typing and environment variable management.
"""

from pathlib import Path
from typing import Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telegram MTProto Client (my.telegram.org)
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    TELEGRAM_PHONE_NUMBER: Optional[str] = None
    TELEGRAM_SESSION_NAME: str = "butler_user_session"

    # Admin Cockpit Bot (t.me/BotFather)
    ADMIN_BOT_TOKEN: str = ""
    ADMIN_USER_ID: int = 0

    # Google Gemini AI Studio (aistudio.google.com)
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"

    # Database & System settings
    DATABASE_PATH: str = "data/butler.db"
    TIMEZONE: str = "Asia/Tashkent"
    AUTO_REPLY_ENABLED: bool = True
    HUMAN_DELAY_MIN: int = 3
    HUMAN_DELAY_MAX: int = 8
    DEBOUNCE_WINDOW_SECONDS: float = 8.0
    DRAFT_EXPIRY_HOURS: int = 24

    # Autonomy modes & Owner ID
    MIGRATE_EXISTING_TO: str = "draft"
    OWNER_ID: Optional[int] = None

    # Smart Digest V2 settings
    DIGEST_V2_ENABLED: bool = True
    DIGEST_LOOKBACK_HOURS: int = 24
    MAX_POSTS_PER_RUN: int = 100
    MAX_POST_CHARS: int = 1500
    RETENTION_DAYS: int = 14
    MAX_AI_CALLS_PER_RUN: int = 10
    BATCH_SIZE: int = 10
    DIGEST_TIME: str = "08:00"
    DEDUPLICATION_SIMILARITY_THRESHOLD: float = 0.82
    SPAM_SCORE_THRESHOLD: float = 0.60
    SPAM_RULES_FILE: str = "data/spam_keywords.json"
    DIGEST_AI_TEMPERATURE: float = 0.0
    DIGEST_IMPORTANCE_MIN_THRESHOLD: int = 2
    DIGEST_TOPIC_LIMIT_DEFAULT: int = 5
    DIGEST_TOPIC_LIMIT_AI: int = 8

    # Contact Memory settings
    MEMORY_ENABLED: bool = True
    MEMORY_LOG_MUTED: bool = False
    MEM_MESSAGE_RETENTION_DAYS: int = 180
    MEM_EMBEDDING_MODEL: str = "gemini-embedding-001"
    MEM_EMBEDDING_DIM: int = 3072
    MEM_MAX_EMBED_CALLS_PER_DAY: int = 200
    MEM_EXTRACT_IDLE_MINUTES: int = 5
    MEM_MAX_EXTRACT_CALLS_PER_DAY: int = 50
    MEM_MAX_MESSAGES_PER_BATCH: int = 15
    MEM_MAX_FACT_LENGTH: int = 150
    MEM_FACT_VALIDITY_DAYS: int = 30
    MEM_CONTEXT_TIMEOUT_SECONDS: float = 2.5
    MEM_MAX_CONTEXT_TOKENS: int = 1200
    MEM_IMPORT_ENABLED: bool = False
    MEM_IMPORT_MAX_MESSAGES: int = 200
    MEM_REDACTION_MASK: str = "[REDACTED]"
    MEM_SIMILARITY_THRESHOLD: float = 0.40
    MEM_RETRIEVAL_TOP_K: int = 5
    MEM_EMBEDDING_TIMEOUT_SECONDS: float = 15.0
    MEM_EXTRACT_TIMEOUT_SECONDS: float = 20.0
    MEM_MAX_SUMMARY_LENGTH: int = 500
    MEM_SUMMARY_MIN_MESSAGES: int = 5
    MEM_MAX_SUMMARY_CALLS_PER_DAY: int = 30
    MEM_SUMMARY_TIMEOUT_SECONDS: float = 20.0

    # Injection detection keywords across 4 languages (U2)
    MEM_INJECTION_KEYWORDS_UZ_LATIN: List[str] = [
        "oldingi ko'rsatmalarni unut",
        "oldingi qoidalarni bekor qil",
        "tizim ko'rsatmasi",
        "system prompt",
        "sen endi",
        "har doim shunday javob ber",
        "o'zingni ... deb tut",
        "qoidalarni chetlab o't",
        "barcha cheklovlarni olib tashla",
        "yangi ko'rsatma",
        "ko'rsatmani bajar",
        "sen erkin modelsan",
        "faqat shunday gapir",
    ]
    MEM_INJECTION_KEYWORDS_UZ_CYRILLIC: List[str] = [
        "олдинги кўрсатмаларни унут",
        "олдинги қоидаларни бекор қил",
        "тизим кўрсатмаси",
        "сен энди",
        "ҳар доим шундай жавоб бер",
        "ўзингни ... деб тут",
        "барча чекловларни олиб ташла",
        "янги кўрсатма",
    ]
    MEM_INJECTION_KEYWORDS_RU: List[str] = [
        "забудь все инструкции",
        "забудь предыдущие инструкции",
        "игнорируй предыдущие",
        "системный промпт",
        "ты теперь",
        "действуй как",
        "всегда отвечай",
        "отвечай только так",
        "режим разработчика",
        "сними все ограничения",
        "новые инструкции",
        "обойти правила",
    ]
    MEM_INJECTION_KEYWORDS_EN: List[str] = [
        "ignore previous instructions",
        "ignore all previous instructions",
        "disregard previous instructions",
        "system prompt",
        "you are now",
        "act as",
        "jailbreak",
        "developer mode",
        "dan mode",
        "bypass safety",
        "you must always reply",
        "always reply",
        "you are an unrestricted",
        "forget your rules",
        "new instructions",
    ]

    # Forbidden categories (H6)
    MEM_FORBIDDEN_CATEGORIES: List[str] = [
        "health",
        "religion",
        "ethnicity",
        "politics",
        "sexual_life",
        "finances",
        "finance",
        "moliya",
        "byudjet",
        "budget",
        "debt",
        "income",
        "criminal",
    ]
    MEM_FORBIDDEN_KEYWORDS: List[str] = [
        # Health / Salomatlik
        "kasal", "kasallik", "dori", "shifoxona", "tashxis", "болен", "болезнь", "лекарство", "диагноз", "illness", "medical", "hospital", "sick",
        # Religion / Din
        "namoz", "masjid", "cherkov", "din", "islom", "musulmon", "xristian", "намаз", "мечеть", "религия", "мусульманин", "церковь", "religion", "mosque", "church", "muslim",
        # Ethnicity / Millat
        "millat", "irq", "millati", "национальность", "раса", "ethnicity", "race",
        # Politics / Siyosat
        "siyosat", "saylov", "prezident", "partiya", "сиёсат", "политика", "выборы", "politics", "election",
        # Finances / Moliya / Byudjet / Qarz / Maosh
        "moliya", "byudjet", "qarz", "qarzim", "oylik", "daromad", "maosh", "kredit", "pul", "молия", "бюджет", "қарз", "маош", "даромад", "кредит", "финансы", "долг", "зарплата", "доход", "finance", "budget", "debt", "salary", "income", "loan",
        # Criminal / Jinoyat
        "sud", "qamoq", "jinoyat", "prokuror", "суд", "тюрьма", "преступление", "jail", "prison", "crime", "criminal",
    ]

    # Retention & Backup policies (Step 1.7 & 1.10)
    CONVERSATION_HISTORY_RETENTION_DAYS: int = 180
    BACKUP_KEEP: int = 5

    # Base path
    BASE_DIR: Path = Path(__file__).resolve().parent

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def owner_id_effective(self) -> int:
        return self.OWNER_ID if self.OWNER_ID is not None else self.ADMIN_USER_ID

    @property
    def db_file_path(self) -> Path:
        path = self.BASE_DIR / self.DATABASE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()

