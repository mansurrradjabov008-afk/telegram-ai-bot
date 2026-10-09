"""
Configuration module for Antigravity Telegram AI Executive Butler.
Uses Pydantic Settings for strict typing and environment variable management.
"""

from pathlib import Path
from typing import Optional
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

