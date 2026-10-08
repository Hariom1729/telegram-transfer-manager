"""Security Middleware and Authorization Verification."""

import logging
from typing import Callable, Coroutine
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select
from app.config import settings
from app.database import get_session
from app.models.user import User

logger = logging.getLogger(__name__)


def check_authorized(func: Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine]):
    """Decorator ensuring that only authorized administrators can interact with the bot."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user:
            return

        user_id = user.id

        # Check against ADMIN_USER_IDS
        if not settings.is_admin(user_id):
            logger.warning("Unauthorized access attempt by user_id=%s username=%s", user_id, user.username)
            unauth_msg = "⛔ You are not authorized to use this bot."
            if update.callback_query:
                await update.callback_query.answer(unauth_msg, show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(unauth_msg)
            return

        # Ensure user is registered in the database
        async with get_session() as session:
            res = await session.execute(
                select(User).where(User.telegram_id == user_id)
            )
            existing = res.scalar_one_or_none()
            if not existing:
                new_user = User(
                    telegram_id=user_id,
                    username=user.username,
                    first_name=user.first_name,
                    is_admin=True,
                )
                session.add(new_user)
            else:
                existing.username = user.username
                existing.first_name = user.first_name

        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            if "not modified" in str(e).lower():
                if update.callback_query:
                    try:
                        await update.callback_query.answer()
                    except Exception:
                        pass
                return
            raise

    return wrapper

