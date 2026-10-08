"""Application Configuration Module."""

import os
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

# Load .env file
load_dotenv()


PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


class Settings:
    """Application settings loaded from environment variables."""

    # Project root
    PROJECT_ROOT: Path = PROJECT_ROOT

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
    _raw_db_url = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///./data/telegram.db"
    ).strip()
    if _raw_db_url.startswith("sqlite+aiosqlite:///./") or _raw_db_url.startswith("sqlite+aiosqlite://./"):
        _rel_db_path = _raw_db_url.split("sqlite+aiosqlite:///")[-1].lstrip("./")
        DATABASE_URL: str = f"sqlite+aiosqlite:///{PROJECT_ROOT / _rel_db_path}"
    else:
        DATABASE_URL: str = _raw_db_url

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

    # Optimized Media Downloader & Uploader Settings
    DOWNLOAD_WORKERS: int = int(os.getenv("DOWNLOAD_WORKERS", "8").strip())
    UPLOAD_WORKERS: int = int(os.getenv("UPLOAD_WORKERS", "4").strip())
    DOWNLOAD_REQUEST_SIZE: int = int(
        os.getenv("DOWNLOAD_REQUEST_SIZE", "524288").strip()
    )
    MAX_DOWNLOAD_RETRIES: int = int(
        os.getenv("MAX_DOWNLOAD_RETRIES", "5").strip()
    )
    DOWNLOAD_PROGRESS_INTERVAL: int = int(
        os.getenv("DOWNLOAD_PROGRESS_INTERVAL", "3").strip()
    )
    DOWNLOAD_RESUME: bool = os.getenv("DOWNLOAD_RESUME", "true").strip().lower() in (
        "true",
        "1",
        "yes",
    )

    # Modern Web Dashboard & Telegram Mini App URL (e.g. https://your-space.hf.space)
    WEBAPP_URL: str = os.getenv("WEBAPP_URL", "").strip()

    # Session Storage (Always absolute path anchored to PROJECT_ROOT)
    _raw_session_dir = os.getenv("SESSION_DIRECTORY", "./data/sessions").strip()
    _session_p = Path(_raw_session_dir)
    SESSION_DIRECTORY: str = str(
        (_session_p if _session_p.is_absolute() else (PROJECT_ROOT / _session_p)).resolve()
    )

    @classmethod
    def ensure_directories(cls) -> None:
        """Ensure necessary data and session directories exist on disk."""
        Path(cls.SESSION_DIRECTORY).mkdir(parents=True, exist_ok=True)
        # Also ensure directory of sqlite database exists if sqlite URL
        if "sqlite" in cls.DATABASE_URL:
            db_part = cls.DATABASE_URL.split(":///")[-1]
            if db_part and not db_part.startswith(":memory:"):
                db_path = Path(db_part).parent
                db_path.mkdir(parents=True, exist_ok=True)
        Path(cls.PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)
        from app.utils.paths import ensure_download_directory, get_temp_download_directory

        ensure_download_directory()
        get_temp_download_directory()

    @classmethod
    def run_startup_diagnostics(cls) -> None:
        """Log storage diagnostic information without logging secrets."""
        import logging
        from app.utils.paths import get_download_directory, get_temp_download_directory

        diag_logger = logging.getLogger("app.diagnostics")
        cls.ensure_directories()

        sess_dir = Path(cls.SESSION_DIRECTORY)
        dir_writable = os.access(sess_dir, os.W_OK)
        diag_logger.info("Telethon session directory: %s", sess_dir)
        diag_logger.info("Directory writable: %s", dir_writable)

        session_files = list(sess_dir.glob("*.session"))
        if session_files:
            for sf in session_files:
                sf_writable = os.access(sf, os.W_OK)
                diag_logger.info("Session file: %s", sf)
                diag_logger.info("Session writable: %s", sf_writable)
        else:
            diag_logger.info("No existing .session files found in session directory.")

        dl_dir = get_download_directory()
        tmp_dir = get_temp_download_directory()
        diag_logger.info("User Downloads directory: %s (writable: %s)", dl_dir, os.access(dl_dir, os.W_OK))
        diag_logger.info("Temporary files directory: %s (writable: %s)", tmp_dir, os.access(tmp_dir, os.W_OK))

        # Crypto acceleration check
        try:
            import cryptg  # noqa: F401
            diag_logger.info("Telethon crypto acceleration: ENABLED")
        except ImportError:
            diag_logger.info("Telethon crypto acceleration: DISABLED")

        diag_logger.info(
            "Download settings: workers=%d, request_size=%d bytes, max_retries=%d, resume=%s",
            cls.DOWNLOAD_WORKERS,
            cls.DOWNLOAD_REQUEST_SIZE,
            cls.MAX_DOWNLOAD_RETRIES,
            cls.DOWNLOAD_RESUME,
        )

        if "sqlite" in cls.DATABASE_URL:
            db_part = cls.DATABASE_URL.split(":///")[-1]
            if db_part and not db_part.startswith(":memory:"):
                db_file = Path(db_part)
                db_dir_writable = os.access(db_file.parent, os.W_OK)
                diag_logger.info("Database path: %s", db_file)
                diag_logger.info("Database directory writable: %s", db_dir_writable)
                if db_file.exists():
                    diag_logger.info("Database file writable: %s", os.access(db_file, os.W_OK))

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

        # Validate & normalize download workers (allowed: 1, 2, 4, 8)
        if cls.DOWNLOAD_WORKERS not in (1, 2, 4, 8):
            cls.DOWNLOAD_WORKERS = 4

        # Validate & normalize download request size (must be multiple of 4096 between 4KB and 512KB)
        cls.DOWNLOAD_REQUEST_SIZE = (cls.DOWNLOAD_REQUEST_SIZE // 4096) * 4096
        if cls.DOWNLOAD_REQUEST_SIZE < 4096:
            cls.DOWNLOAD_REQUEST_SIZE = 4096
        elif cls.DOWNLOAD_REQUEST_SIZE > 524288:
            cls.DOWNLOAD_REQUEST_SIZE = 524288

        if cls.MAX_DOWNLOAD_RETRIES < 1:
            cls.MAX_DOWNLOAD_RETRIES = 5

        if cls.DOWNLOAD_PROGRESS_INTERVAL < 1:
            cls.DOWNLOAD_PROGRESS_INTERVAL = 3


settings = Settings()


