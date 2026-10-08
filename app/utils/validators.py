"""Input validation utilities."""

import re
from typing import Optional, Tuple


def validate_phone_number(phone: str) -> Tuple[bool, str]:
    """Validate international phone number format (+123456789...)."""
    cleaned = phone.strip().replace(" ", "").replace("-", "")
    if not re.match(r"^\+[1-9]\d{6,14}$", cleaned):
        return False, "Invalid phone number format. Please use international format: +1234567890"
    return True, cleaned


def validate_chat_identifier(identifier: str) -> Tuple[bool, Optional[str | int]]:
    """Validate chat username, invite link or numeric ID."""
    raw = identifier.strip()
    # Check if purely numeric or negative numeric (e.g. -100123456789)
    if re.match(r"^-?\d+$", raw):
        return True, int(raw)

    # Check if Telegram username (e.g. @channel or channel)
    clean_username = raw.lstrip("@")
    if re.match(r"^[a-zA-Z0-9_]{4,32}$", clean_username):
        return True, f"@{clean_username}"

    # Telegram link: t.me/username or t.me/c/1234567890/1
    if "t.me/" in raw:
        path = raw.split("t.me/")[-1].strip("/")
        if path.startswith("c/"):
            parts = path.split("/")
            if len(parts) >= 2 and parts[1].isdigit():
                # Internal channel ID
                return True, int(f"-100{parts[1]}")
        else:
            username = path.split("/")[0]
            if username:
                return True, f"@{username}"

    return False, None


def validate_message_range(
    start_id: Optional[int], end_id: Optional[int]
) -> Tuple[bool, str]:
    """Validate message ID range."""
    if start_id is not None and start_id < 1:
        return False, "Start message ID must be at least 1."
    if end_id is not None and end_id < 1:
        return False, "End message ID must be at least 1."
    if (
        start_id is not None
        and end_id is not None
        and start_id > end_id
    ):
        return False, "Start message ID cannot be greater than End message ID."
    return True, ""

