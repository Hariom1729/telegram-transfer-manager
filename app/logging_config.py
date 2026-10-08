"""Structured Logging Configuration with Sensitive Data Sanitization."""

import logging
import logging.handlers
import re
from pathlib import Path
from app.config import settings


class SecretRedactingFilter(logging.Filter):
    """Filter that masks sensitive tokens, hashes, and authorization codes in log records."""

    def __init__(self, secrets_to_mask=None):
        super().__init__()
        self.secrets_to_mask = [s for s in (secrets_to_mask or []) if s and len(s) > 4]

    def _sanitize_string(self, text: str) -> str:
        """Sanitize a single string by masking sensitive secrets, tokens, codes, and phone numbers."""
        for secret in self.secrets_to_mask:
            if secret in text:
                text = text.replace(secret, "[REDACTED_SECRET]")

        # Bot tokens (e.g. 123456789:ABCdef...)
        text = re.sub(
            r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b", "[REDACTED_BOT_TOKEN]", text
        )
        # Phone numbers in international format (e.g. +12345678901)
        text = re.sub(
            r"\+[1-9]\d{6,14}\b", "[REDACTED_PHONE]", text
        )
        # Login codes and passwords
        text = re.sub(
            r"(code|password|hash|session)[:=\s]+[0-9a-zA-Z!@#$%^&*()_+]{4,}",
            r"\1: [REDACTED]",
            text,
            flags=re.IGNORECASE,
        )
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._sanitize_string(record.msg)

        if record.args:
            if isinstance(record.args, tuple):
                new_args = []
                for a in record.args:
                    if isinstance(a, str):
                        new_args.append(self._sanitize_string(a))
                    else:
                        new_args.append(a)
                record.args = tuple(new_args)
            elif isinstance(record.args, dict):
                record.args = {
                    k: (self._sanitize_string(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        return True


def setup_logging() -> logging.Logger:
    """Configure structured logging for the application."""
    Path("./logs").mkdir(parents=True, exist_ok=True)
    log_level = getattr(logging, settings.LOG_LEVEL, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Avoid duplicate handlers on re-initialization
    if root_logger.handlers:
        root_logger.handlers.clear()

    # Formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Secrets filter
    secrets_filter = SecretRedactingFilter(
        secrets_to_mask=[settings.BOT_TOKEN, settings.API_HASH]
    )

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(secrets_filter)
    root_logger.addHandler(console_handler)

    # File Handler
    file_handler = logging.handlers.RotatingFileHandler(
        filename="./logs/transfer_manager.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(secrets_filter)
    root_logger.addHandler(file_handler)

    # Suppress verbose noisy logging from external libraries
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    logger = logging.getLogger("app")
    logger.info("Logging configured. Level: %s", settings.LOG_LEVEL)
    return logger

