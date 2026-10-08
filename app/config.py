"""Application Configuration Module."""

import os
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

# Load .env file
load_dotenv()


class Settings:
    """Application settings loaded from environment variables."""

    # Telegram Bot Token
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()

    # Telegram MTProto Credentials
    _raw_api_id = os.getenv("API_ID", "").strip()
    API_ID: Optional[int] = int(_raw_api_id) if _raw_api_id.isdigit() else None
    API_HASH: str = os.getenv("API_HASH", "").strip()

    # Authorized Telegram Admin IDs
    _admin_ids_raw = os.getenv("ADMIN_USER_IDS", "").strip()
    ADMIN_USER_IDS: List[int] = [
        int(uid.strip())
        for uid in _admin_ids_raw.split(",")
        if uid.strip() and uid.strip().lstrip("-").isdigit()
    ]

    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///./data/telegram.db"
    ).strip()

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    # Transfer Settings
    MAX_CONCURRENT_TRANSFERS: int = int(
        os.getenv("MAX_CONCURRENT_TRANSFERS", "2").strip()
    )
    MAX_RETRY_ATTEMPTS: int = int(os.getenv("MAX_RETRY_ATTEMPTS", "5").strip())
    PROGRESS_UPDATE_INTERVAL: int = int(
        os.getenv("PROGRESS_UPDATE_INTERVAL", "3").strip()
    )

    # Session Storage
    SESSION_DIRECTORY: str = os.getenv(
        "SESSION_DIRECTORY", "./data/sessions"
    ).strip()

    @classmethod
    def ensure_directories(cls) -> None:
        """Ensure necessary data and session directories exist on disk."""
        Path(cls.SESSION_DIRECTORY).mkdir(parents=True, exist_ok=True)
        # Also ensure directory of sqlite database exists if sqlite URL
        if "sqlite" in cls.DATABASE_URL:
            # Parse path from sqlite URL
            # e.g., sqlite+aiosqlite:///./data/telegram.db -> ./data/telegram.db
            db_part = cls.DATABASE_URL.split(":///")[-1]
            if db_part and not db_part.startswith(":memory:"):
                db_path = Path(db_part).parent
                db_path.mkdir(parents=True, exist_ok=True)
        Path("./logs").mkdir(parents=True, exist_ok=True)

    @classmethod
    def is_admin(cls, user_id: int) -> bool:
        """Check if a Telegram user ID is in the admin allowlist.
        If no ADMIN_USER_IDS are configured, all users are blocked by default for security.
        """
        if not cls.ADMIN_USER_IDS:
            return False
        return user_id in cls.ADMIN_USER_IDS

    @classmethod
    def validate(cls) -> None:
        """Validate required configuration at startup."""
        if not cls.BOT_TOKEN:
            raise ValueError(
                "BOT_TOKEN is missing! Set it in your environment or .env file."
            )


settings = Settings()

