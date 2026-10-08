"""Telegram Bot API Client Helper."""

import logging
from typing import Optional
from telegram import Bot
from app.config import settings

logger = logging.getLogger(__name__)


class BotClientHelper:
    """Helper methods using the Telegram Bot API client."""

    def __init__(self, bot: Optional[Bot] = None):
        self._bot = bot

    @property
    def bot(self) -> Bot:
        """Get or lazily initialize the Telegram Bot instance."""
        if self._bot is None:
            if not settings.BOT_TOKEN:
                raise ValueError("BOT_TOKEN is not configured.")
            self._bot = Bot(token=settings.BOT_TOKEN)
        return self._bot

    def set_bot(self, bot: Bot) -> None:
        """Inject bot instance."""
        self._bot = bot

    async def send_or_copy_message(
        self,
        destination_chat_id: int,
        from_chat_id: int,
        message_id: int,
        thread_id: Optional[int] = None,
    ) -> int:
        """Copy a message to destination chat and optional forum topic thread using Bot API.

        Uses copy_message to re-send without forward header.
        """
        copied = await self.bot.copy_message(
            chat_id=destination_chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            message_thread_id=thread_id,
        )
        return copied.message_id

    async def send_text(
        self,
        chat_id: int,
        text: str,
        thread_id: Optional[int] = None,
        reply_markup=None,
    ) -> int:
        """Send a text message to chat/topic."""
        msg = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        return msg.message_id


bot_client_helper = BotClientHelper()

