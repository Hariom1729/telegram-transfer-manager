"""State constants and user session state storage."""

from typing import Any, Dict


class BotState:
    """State identifiers for interactive conversation flows."""

    IDLE = "IDLE"

    # Account connection states
    AUTH_WAITING_PHONE = "AUTH_WAITING_PHONE"
    AUTH_WAITING_CODE = "AUTH_WAITING_CODE"
    AUTH_WAITING_2FA = "AUTH_WAITING_2FA"

    # Transfer wizard states
    WIZARD_ACCOUNT_SELECT = "WIZARD_ACCOUNT_SELECT"
    WIZARD_SOURCE_SELECT = "WIZARD_SOURCE_SELECT"
    WIZARD_SOURCE_INPUT = "WIZARD_SOURCE_INPUT"
    WIZARD_DEST_SELECT = "WIZARD_DEST_SELECT"
    WIZARD_DEST_INPUT = "WIZARD_DEST_INPUT"
    WIZARD_TOPIC_SELECT = "WIZARD_TOPIC_SELECT"
    WIZARD_TOPIC_CREATE = "WIZARD_TOPIC_CREATE"
    WIZARD_CONTENT_SELECT = "WIZARD_CONTENT_SELECT"
    WIZARD_RANGE_SELECT = "WIZARD_RANGE_SELECT"
    WIZARD_RANGE_INPUT = "WIZARD_RANGE_INPUT"
    WIZARD_BROWSE_MESSAGES = "WIZARD_BROWSE_MESSAGES"
    WIZARD_BROWSE_JUMP = "WIZARD_BROWSE_JUMP"
    WIZARD_BROWSE_SEARCH = "WIZARD_BROWSE_SEARCH"
    WIZARD_DUPLICATE_SELECT = "WIZARD_DUPLICATE_SELECT"
    WIZARD_PREVIEW = "WIZARD_PREVIEW"

    # Single message direct forwarding
    DIRECT_MSG_DEST_SELECT = "DIRECT_MSG_DEST_SELECT"
    DIRECT_MSG_TOPIC_SELECT = "DIRECT_MSG_TOPIC_SELECT"

    # Live sync states
    SYNC_SOURCE_SELECT = "SYNC_SOURCE_SELECT"
    SYNC_DEST_SELECT = "SYNC_DEST_SELECT"
    SYNC_TOPIC_SELECT = "SYNC_TOPIC_SELECT"

    # Cleaning & duplicate removal states
    CLEAN_RANGE_INPUT = "CLEAN_RANGE_INPUT"


class UserSessionStore:
    """In-memory session state store for user interactions."""

    def __init__(self) -> None:
        self._user_states: Dict[int, str] = {}
        self._user_data: Dict[int, Dict[str, Any]] = {}

    def get_state(self, user_id: int) -> str:
        """Get current state of user."""
        return self._user_states.get(user_id, BotState.IDLE)

    def set_state(self, user_id: int, state: str) -> None:
        """Set user state."""
        self._user_states[user_id] = state

    def get_data(self, user_id: int) -> Dict[str, Any]:
        """Get temporary data payload for user wizard."""
        if user_id not in self._user_data:
            self._user_data[user_id] = {}
        return self._user_data[user_id]

    def update_data(self, user_id: int, **kwargs) -> None:
        """Update user session data."""
        data = self.get_data(user_id)
        data.update(kwargs)

    def clear(self, user_id: int) -> None:
        """Clear user state and temporary data."""
        self._user_states.pop(user_id, None)
        self._user_data.pop(user_id, None)


session_store = UserSessionStore()

