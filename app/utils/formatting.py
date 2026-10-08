"""Formatting utilities for Telegram Transfer Manager UI."""

import datetime
from typing import Optional


def format_progress_bar(current: int, total: int, length: int = 15) -> str:
    """Generate a Unicode progress bar: ████████████░░░ 82%."""
    if total <= 0:
        percent = 0.0
    else:
        percent = min(100.0, max(0.0, (current / total) * 100.0))

    filled_length = int(length * (percent / 100.0))
    bar = "█" * filled_length + "░" * (length - filled_length)
    return f"{bar} {int(percent)}%"


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format, e.g. 18m 42s or 1h 05m 12s."""
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m {sec:02d}s"
    return f"{minutes}m {sec:02d}s"


def format_chat_display(
    chat_type: str, title: str, chat_id: Optional[int] = None
) -> str:
    """Format a chat title with an appropriate icon based on type."""
    icon = "📢"
    if chat_type == "supergroup" or chat_type == "group":
        icon = "👥"
    elif chat_type == "private":
        icon = "🔒"
    elif chat_type == "channel":
        icon = "📢"

    if chat_id:
        return f"{icon} {title} (`{chat_id}`)"
    return f"{icon} {title}"


def format_user_friendly_error(raw_error: Exception | str) -> str:
    """Translate technical MTProto/Bot API errors into clear, actionable explanations."""
    err_str = str(raw_error)

    if "FloodWait" in err_str:
        return (
            "⏳ Telegram rate limit reached (FloodWait).\n"
            "The system is waiting automatically before continuing."
        )
    if "ChatWriteForbidden" in err_str or "ChatAdminRequired" in err_str:
        return (
            "❌ Telegram rejected this operation.\n\n"
            "Possible reasons:\n"
            "• The account doesn't have permission to write in this chat.\n"
            "• Admin rights are required.\n"
            "• Check the destination permissions and try again."
        )
    if "TopicClosed" in err_str or "ForumClosed" in err_str:
        return (
            "❌ This forum topic is closed.\n\n"
            "Please select an open topic or reopen the topic in Telegram."
        )
    if "MessageIdInvalid" in err_str:
        return "❌ The requested message was deleted or is no longer accessible."
    if "UserBannedInChannel" in err_str or "ChannelPrivate" in err_str:
        return (
            "❌ Cannot access this channel.\n\n"
            "Possible reasons:\n"
            "• The account is not a member of the private chat.\n"
            "• The account was restricted or banned."
        )
    if "FileReferenceExpired" in err_str:
        return "⚠️ File reference expired. Re-fetching media..."

    return f"❌ Operation failed: {err_str}"

