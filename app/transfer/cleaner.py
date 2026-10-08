"""Chat cleaner and duplicate removal service for channels and groups.

Features:
- Scans channels/groups for duplicate media (by file size, name, media hash) and duplicate text.
- Deletes duplicate messages while preserving the canonical original copy.
- Deletes all media files (videos, documents, audio, photos) leaving text intact.
- Purges messages by ID range or recent counts.
- Strict confirmation prompts to prevent accidental deletions.
- Batch deletion (up to 100 IDs per request) with FloodWait and permission handling.
- Cleans up corresponding database MessageMapping records.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import math
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlalchemy import delete
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telethon import TelegramClient
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    RpcCallFailError,
    UserAdminInvalidError,
)
from telethon.tl.custom.message import Message

from app.bot.states import BotState, session_store
from app.database import get_session
from app.models.message_mapping import MessageMapping
from app.telegram.topics import TopicManager
from app.telegram.user_client import user_client_manager


logger = logging.getLogger(__name__)



@dataclass
class DuplicateScanResult:
    """Outcome of duplicate content analysis in a channel or group."""

    chat_id: int
    chat_title: str
    total_scanned: int
    duplicate_ids: List[int]
    duplicate_media_count: int
    duplicate_text_count: int
    total_duplicate_bytes: int


class ChatCleanerService:
    """Scans and removes duplicate content, media files, or bulk messages from Telegram chats."""

    @staticmethod
    def _extract_media_key(msg: Message) -> Optional[Tuple]:
        """Generate a deterministic fingerprint for media comparison."""
        if not getattr(msg, "media", None):
            return None

        # Documents (videos, files, audio)
        doc = getattr(msg, "document", None)
        if doc:
            file_name = ""
            for attr in getattr(doc, "attributes", []):
                if hasattr(attr, "file_name"):
                    file_name = attr.file_name or ""
            return ("doc", getattr(doc, "size", 0), file_name)

        # Photos
        photo = getattr(msg, "photo", None)
        if photo:
            return ("photo", getattr(photo, "id", 0))

        # Generic media fallback
        return ("media", getattr(msg, "file", None).size if getattr(msg, "file", None) else 0)

    @classmethod
    async def scan_duplicates(
        cls,
        client: TelegramClient,
        chat_id: int,
        chat_title: str = "",
        limit: int = 500,
        topic_id: Optional[int] = None,
    ) -> DuplicateScanResult:
        """Scan messages in chat/topic and detect duplicate media and duplicate text."""
        seen_keys: Dict[Tuple, int] = {}
        duplicate_ids: List[int] = []
        dup_media_count = 0
        dup_text_count = 0
        dup_bytes = 0
        total_scanned = 0

        iter_kwargs = {"limit": limit, "reverse": True}
        if topic_id:
            iter_kwargs["reply_to"] = topic_id

        async for msg in client.iter_messages(chat_id, **iter_kwargs):
            if not msg or not msg.id:
                continue

            if topic_id:
                msg_top = getattr(getattr(msg, "reply_to", None), "reply_to_top_id", None) or getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
                if msg.id != topic_id and msg_top != topic_id:
                    continue

            total_scanned += 1

            # 1. Media fingerprinting
            media_key = cls._extract_media_key(msg)
            if media_key:
                if media_key in seen_keys:
                    duplicate_ids.append(msg.id)
                    dup_media_count += 1
                    # Track bytes
                    if media_key[0] == "doc":
                        dup_bytes += media_key[1]
                else:
                    seen_keys[media_key] = msg.id
                continue

            # 2. Text fingerprinting (ignore empty / whitespace)
            raw_text = (msg.message or "").strip()
            if raw_text and len(raw_text) >= 5:
                text_key = ("text", raw_text)
                if text_key in seen_keys:
                    duplicate_ids.append(msg.id)
                    dup_text_count += 1
                else:
                    seen_keys[text_key] = msg.id

        return DuplicateScanResult(
            chat_id=chat_id,
            chat_title=chat_title,
            total_scanned=total_scanned,
            duplicate_ids=duplicate_ids,
            duplicate_media_count=dup_media_count,
            duplicate_text_count=dup_text_count,
            total_duplicate_bytes=dup_bytes,
        )

    @classmethod
    async def scan_media_files(
        cls,
        client: TelegramClient,
        chat_id: int,
        limit: int = 500,
        topic_id: Optional[int] = None,
    ) -> Tuple[List[int], int]:
        """Scan chat/topic and collect all message IDs that contain media files."""
        media_ids: List[int] = []
        total_bytes = 0

        iter_kwargs = {"limit": limit}
        if topic_id:
            iter_kwargs["reply_to"] = topic_id

        async for msg in client.iter_messages(chat_id, **iter_kwargs):
            if not msg or not msg.id:
                continue

            if topic_id:
                msg_top = getattr(getattr(msg, "reply_to", None), "reply_to_top_id", None) or getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
                if msg.id != topic_id and msg_top != topic_id:
                    continue

            if getattr(msg, "media", None):
                media_ids.append(msg.id)
                doc = getattr(msg, "document", None)
                if doc:
                    total_bytes += getattr(doc, "size", 0)

        return media_ids, total_bytes

    @classmethod
    async def delete_topic(
        cls,
        client: TelegramClient,
        chat_id: int,
        topic_id: int,
    ) -> bool:
        """Permanently delete a forum topic and all its message history."""
        from telethon.tl.functions.messages import DeleteTopicHistoryRequest

        try:
            entity = await client.get_entity(chat_id)
            await client(DeleteTopicHistoryRequest(peer=entity, top_msg_id=topic_id))
        except (ChatAdminRequiredError, UserAdminInvalidError) as pe:
            raise RuntimeError(
                "Your connected Telegram account requires admin permissions with "
                "'Manage Topics' or 'Delete Messages' right to delete this topic."
            ) from pe
        except FloodWaitError as fe:
            await asyncio.sleep(min(fe.seconds, 10))
            await client(DeleteTopicHistoryRequest(peer=chat_id, top_msg_id=topic_id))

        # Clean database message mappings for this topic
        try:
            async with get_session() as session:
                await session.execute(
                    delete(MessageMapping).where(
                        MessageMapping.destination_chat_id == chat_id,
                        MessageMapping.destination_thread_id == topic_id,
                    )
                )
        except Exception as dbe:
            logger.debug("Could not clean message mappings for topic: %s", dbe)

        return True


    @classmethod
    async def delete_messages_batch(
        cls,
        client: TelegramClient,
        chat_id: int,
        message_ids: List[int],
    ) -> int:
        """Delete messages in batches of up to 100 with FloodWait and permission handling."""
        if not message_ids:
            return 0

        deleted_count = 0
        chunk_size = 100

        for i in range(0, len(message_ids), chunk_size):
            chunk = message_ids[i : i + chunk_size]
            try:
                await client.delete_messages(chat_id, chunk, revoke=True)
                deleted_count += len(chunk)
                await asyncio.sleep(0.3)
            except FloodWaitError as fe:
                logger.warning("Telegram FloodWait during deletion: %ss", fe.seconds)
                await asyncio.sleep(min(fe.seconds, 10))
                try:
                    await client.delete_messages(chat_id, chunk, revoke=True)
                    deleted_count += len(chunk)
                except Exception:
                    pass
            except (ChatAdminRequiredError, UserAdminInvalidError) as pe:
                logger.error("Admin permissions required to delete messages in chat %s: %s", chat_id, pe)
                raise RuntimeError(
                    "Your connected Telegram account does not have admin permissions with "
                    "'Delete Messages' right in this channel/group."
                ) from pe
            except Exception as e:
                logger.error("Failed to delete chunk in chat %s: %s", chat_id, e)

        # Clean database message mappings for deleted messages
        try:
            async with get_session() as session:
                await session.execute(
                    delete(MessageMapping).where(
                        MessageMapping.destination_chat_id == chat_id,
                        MessageMapping.destination_message_id.in_(message_ids),
                    )
                )
        except Exception as dbe:
            logger.debug("Could not clean message mappings: %s", dbe)

        return deleted_count


chat_cleaner_service = ChatCleanerService()


# =============================================================================
# UI Presentation & Callback Handlers
# =============================================================================

async def show_clean_menu(
    query_or_message: Any,
    user_id: int,
    chat_id: int,
    chat_title: str,
    topic_id: Optional[int] = None,
    topic_title: Optional[str] = None,
) -> None:
    """Display the Cleaning & Duplicate Management menu for a chosen chat or topic."""
    if topic_id is not None:
        text = (
            "🧹 *Topic Cleaning & Management*\n\n"
            f"Target Chat: *{chat_title}*\n"
            f"🧵 Topic: *{topic_title or f'Topic #{topic_id}'}*\n"
            f"`Topic ID: {topic_id}`\n\n"
            "Choose an action for this topic:"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🗑️ Delete Entire Topic & History",
                        callback_data=f"clean:prompt_del_topic:{chat_id}:{topic_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔍 Scan & Remove Duplicates in Topic",
                        callback_data=f"clean:scan_dedup_topic:{chat_id}:{topic_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📁 Delete All Media in Topic",
                        callback_data=f"clean:prompt_media_topic:{chat_id}:{topic_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "💥 Purge Messages by Range in Topic",
                        callback_data=f"clean:prompt_range_topic:{chat_id}:{topic_id}",
                    )
                ],
                [InlineKeyboardButton("⬅️ Back to Topics", callback_data=f"clean:topics_list:{chat_id}")],
                [InlineKeyboardButton("🏠 Menu Dashboard", callback_data="nav:home")],
            ]
        )
    else:
        # Check if chat is a forum with topics
        topics = []
        try:
            udata = session_store.get_data(user_id)
            acc_id = udata.get("account_id")
            if not acc_id:
                accs = await user_client_manager.list_user_accounts(user_id)
                if accs:
                    acc_id = accs[0].id
            client = await user_client_manager.get_active_client(acc_id) if acc_id else None
            if client and client.is_connected():
                topics = await TopicManager.get_topics(client, chat_id)
        except Exception as te:
            logger.debug("Chat %s topics check note: %s", chat_id, te)
            topics = []

        if topics:
            session_store.update_data(user_id, clean_available_topics=topics)

        buttons = []
        if topics:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"🧵 Manage / Delete Topics ({len(topics)})",
                        callback_data=f"clean:topics_list:{chat_id}",
                    )
                ]
            )

        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        "🔍 Scan & Remove Duplicates" + (" (Entire Group)" if topics else ""),
                        callback_data=f"clean:scan_dedup:{chat_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📁 Delete All Media Files" + (" (Entire Group)" if topics else ""),
                        callback_data=f"clean:prompt_media:{chat_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "💥 Purge Messages by Range",
                        callback_data=f"clean:prompt_range:{chat_id}",
                    )
                ],
                [InlineKeyboardButton("⬅️ Back to Chats", callback_data="cp:src:menu")],
                [InlineKeyboardButton("🏠 Menu Dashboard", callback_data="nav:home")],
            ]
        )
        text = (
            "🧹 *Cleaning & Duplicate Manager*\n\n"
            f"Target Chat: *{chat_title}*\n"
            f"`ID: {chat_id}`\n\n"
            "Choose an action to perform on this chat:"
        )
        kb = InlineKeyboardMarkup(buttons)

    if hasattr(query_or_message, "edit_message_text"):
        await query_or_message.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
    else:
        await query_or_message.reply_text(text=text, reply_markup=kb, parse_mode="Markdown")



async def handle_clean_callback(
    update: Update,
    user_id: int,
    data: str,
) -> None:
    """Handle callback queries originating from clean & dedup interactions."""
    query = update.callback_query
    if not query:
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    chat_id = int(parts[2]) if len(parts) > 2 else 0

    udata = session_store.get_data(user_id)
    chat_title = udata.get("clean_chat_title") or str(chat_id)
    account_id = udata.get("account_id")

    if not account_id:
        accounts = await user_client_manager.list_user_accounts(user_id)
        if accounts:
            account_id = accounts[0].id

    client = await user_client_manager.get_active_client(account_id) if account_id else None
    if not client or not client.is_connected():
        await query.edit_message_text(
            "❌ Connected Telegram account session is disconnected. Reconnect via /accounts.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]),
        )
        return

    # 1. Scan for duplicates
    if action == "scan_dedup":
        await query.edit_message_text(
            f"🔍 *Scanning '{chat_title}' for duplicates...*\n\nAnalyzing media fingerprints and messages...",
            parse_mode="Markdown",
        )
        try:
            result = await chat_cleaner_service.scan_duplicates(
                client=client, chat_id=chat_id, chat_title=chat_title, limit=500
            )
        except Exception as e:
            logger.error("Duplicate scan failed for chat %s: %s", chat_id, e)
            await query.edit_message_text(
                f"❌ Unable to scan chat.\n\nReason:\n{e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]),
            )
            return

        if not result.duplicate_ids:
            text = (
                "✅ *No Duplicates Found!*\n\n"
                f"Scanned {result.total_scanned} messages in *{chat_title}*.\n"
                "All media files and messages are unique."
            )
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🧹 Clean Menu", callback_data=f"clean:menu:{chat_id}")],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]
            )
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
            return

        mb_saved = result.total_duplicate_bytes / (1024 * 1024)
        session_store.update_data(
            user_id,
            pending_delete_ids=result.duplicate_ids,
            clean_chat_id=chat_id,
            clean_chat_title=chat_title,
        )

        text = (
            "🧹 *Duplicate Scan Complete*\n\n"
            f"• *Chat:* {chat_title}\n"
            f"• *Scanned Messages:* {result.total_scanned}\n"
            f"• *Duplicates Found:* **{len(result.duplicate_ids)}**\n"
            f"  - Duplicate Media: {result.duplicate_media_count} (~{mb_saved:.1f} MB)\n"
            f"  - Duplicate Text: {result.duplicate_text_count}\n\n"
            f"⚠️ *Keep original copies and delete the {len(result.duplicate_ids)} redundant duplicates?*\n"
            "The oldest original message of each file will be preserved."
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🗑️ Delete {len(result.duplicate_ids)} Duplicates",
                        callback_data=f"clean:exec_dedup:{chat_id}",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="nav:home")],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    # 2. Execute duplicate deletion
    elif action == "exec_dedup":
        pending_ids = udata.get("pending_delete_ids") or []
        if not pending_ids:
            await query.edit_message_text("❌ No pending duplicates found to delete.")
            return

        await query.edit_message_text(
            f"🗑️ *Deleting {len(pending_ids)} duplicates in '{chat_title}'...*\n\nPlease wait...",
            parse_mode="Markdown",
        )
        try:
            del_count = await chat_cleaner_service.delete_messages_batch(client, chat_id, pending_ids)
            session_store.update_data(user_id, pending_delete_ids=[])
            text = (
                f"✅ *Successfully Cleaned Duplicates!*\n\n"
                f"Removed **{del_count}** duplicate message(s) from *{chat_title}*.\n"
                "The original copies remain untouched."
            )
        except RuntimeError as re:
            text = f"❌ {re}"
        except Exception as e:
            text = f"❌ Failed to delete duplicates: {e}"

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Dashboard", callback_data="nav:home")]])
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    # 3. Prompt for Delete All Media Files
    elif action == "prompt_media":
        await query.edit_message_text(
            f"🔍 *Scanning '{chat_title}' for media files...*",
            parse_mode="Markdown",
        )
        try:
            media_ids, total_bytes = await chat_cleaner_service.scan_media_files(client, chat_id, limit=500)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to scan media: {e}")
            return

        if not media_ids:
            text = f"ℹ️ *No media files found* in *{chat_title}*."
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🧹 Clean Menu", callback_data=f"clean:menu:{chat_id}")],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]
            )
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
            return

        mb_total = total_bytes / (1024 * 1024)
        session_store.update_data(
            user_id,
            pending_delete_ids=media_ids,
            clean_chat_id=chat_id,
            clean_chat_title=chat_title,
        )

        text = (
            "📁 *Delete All Media Files*\n\n"
            f"• *Chat:* {chat_title}\n"
            f"• *Media Files Found:* **{len(media_ids)}** (~{mb_total:.1f} MB)\n\n"
            f"⚠️ *Permanently delete all {len(media_ids)} media files?*\n"
            "Text messages will NOT be deleted. This cannot be undone."
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"💥 Yes, Delete {len(media_ids)} Media Files",
                        callback_data=f"clean:exec_media:{chat_id}",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="nav:home")],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    # 4. Execute media files deletion
    elif action == "exec_media":
        pending_ids = udata.get("pending_delete_ids") or []
        if not pending_ids:
            await query.edit_message_text("❌ No pending media files found.")
            return

        await query.edit_message_text(
            f"🗑️ *Deleting {len(pending_ids)} media files in '{chat_title}'...*\n\nPlease wait...",
            parse_mode="Markdown",
        )
        try:
            del_count = await chat_cleaner_service.delete_messages_batch(client, chat_id, pending_ids)
            session_store.update_data(user_id, pending_delete_ids=[])
            text = (
                f"✅ *Media Deletion Complete!*\n\n"
                f"Deleted **{del_count}** media file(s) from *{chat_title}*."
            )
        except RuntimeError as re:
            text = f"❌ {re}"
        except Exception as e:
            text = f"❌ Failed to delete media: {e}"

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu Dashboard", callback_data="nav:home")]])
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    # 5. Prompt for Range Purge
    elif action == "prompt_range":
        session_store.set_state(user_id, BotState.CLEAN_RANGE_INPUT)
        session_store.update_data(user_id, clean_chat_id=chat_id, clean_chat_title=chat_title)
        text = (
            "💥 *Purge Messages by Range*\n\n"
            f"Target Chat: *{chat_title}*\n\n"
            "Please enter the start and end message ID to delete:\n\n"
            "Examples:\n"
            "• `1 50` → deletes messages from ID 1 to 50\n"
            "• `100 250` → deletes messages 100 to 250\n\n"
            "Send /cancel to abort."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="nav:home")]])
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    # 6. Execute range deletion
    elif action == "exec_range":
        pending_ids = udata.get("pending_delete_ids") or []
        if not pending_ids:
            await query.edit_message_text("❌ No pending messages found to delete.")
            return

        await query.edit_message_text(
            f"🗑️ *Deleting {len(pending_ids)} messages in '{chat_title}'...*\n\nPlease wait...",
            parse_mode="Markdown",
        )
        try:
            del_count = await chat_cleaner_service.delete_messages_batch(client, chat_id, pending_ids)
            session_store.update_data(user_id, pending_delete_ids=[])
            text = (
                f"✅ *Range Purge Complete!*\n\n"
                f"Deleted **{del_count}** message(s) from *{chat_title}*."
            )
        except RuntimeError as re:
            text = f"❌ {re}"
        except Exception as e:
            text = f"❌ Failed to delete messages: {e}"

    # 7. Topic-specific cleaning and deletion actions
    elif action == "topics_list":
        topics = udata.get("clean_available_topics") or []
        if not topics and client:
            try:
                topics = await TopicManager.get_topics(client, chat_id)
                session_store.update_data(user_id, clean_available_topics=topics)
            except Exception as te:
                logger.error("Failed to load topics: %s", te)

        if not topics:
            await query.edit_message_text(
                f"ℹ️ No topics found in *{chat_title}*.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"clean:menu:{chat_id}")]]),
                parse_mode="Markdown",
            )
            return

        text = (
            f"🧵 *Topics in {chat_title}*\n\n"
            f"Found {len(topics)} topic(s).\n"
            "Select a topic to manage, clean, or delete:"
        )
        buttons = [
            [
                InlineKeyboardButton(
                    f"🧵 {t.title}",
                    callback_data=f"clean:topic_menu:{chat_id}:{t.id}",
                )
            ]
            for t in topics[:20]
        ]
        buttons.append([InlineKeyboardButton("⬅️ Back to Group Cleaning", callback_data=f"clean:menu:{chat_id}")])
        buttons.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return

    elif action == "topic_menu":
        topic_id = int(parts[3]) if len(parts) > 3 else 0
        topics = udata.get("clean_available_topics") or []
        topic_name = next((t.title for t in topics if t.id == topic_id), f"Topic #{topic_id}")
        session_store.update_data(user_id, clean_topic_id=topic_id, clean_topic_title=topic_name)
        await show_clean_menu(query, user_id, chat_id, chat_title, topic_id=topic_id, topic_title=topic_name)
        return

    elif action == "prompt_del_topic":
        topic_id = int(parts[3]) if len(parts) > 3 else 0
        topics = udata.get("clean_available_topics") or []
        topic_name = next((t.title for t in topics if t.id == topic_id), f"Topic #{topic_id}")
        text = (
            f"⚠️ *Confirm Delete Topic*\n\n"
            f"• *Chat:* {chat_title}\n"
            f"• *Topic:* 🧵 **{topic_name}** (`ID: {topic_id}`)\n\n"
            "Are you sure you want to permanently delete this topic and ALL messages within it?\n\n"
            "💥 **This action cannot be undone.**"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💥 Yes, Permanently Delete Topic",
                        callback_data=f"clean:exec_del_topic:{chat_id}:{topic_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data=f"clean:topic_menu:{chat_id}:{topic_id}",
                    )
                ],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    elif action == "exec_del_topic":
        topic_id = int(parts[3]) if len(parts) > 3 else 0
        topics = udata.get("clean_available_topics") or []
        topic_name = next((t.title for t in topics if t.id == topic_id), f"Topic #{topic_id}")
        await query.edit_message_text(
            f"🗑️ *Deleting topic '🧵 {topic_name}' and its history...*\n\nPlease wait...",
            parse_mode="Markdown",
        )
        try:
            await chat_cleaner_service.delete_topic(client, chat_id, topic_id)
            updated_topics = [t for t in topics if t.id != topic_id]
            session_store.update_data(user_id, clean_available_topics=updated_topics)
            text = (
                f"✅ *Topic Deleted Successfully!*\n\n"
                f"Topic **{topic_name}** and all its messages have been removed from *{chat_title}*."
            )
        except RuntimeError as re:
            text = f"❌ {re}"
        except Exception as e:
            text = f"❌ Failed to delete topic: {e}"

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🧵 Back to Topics", callback_data=f"clean:topics_list:{chat_id}")],
                [InlineKeyboardButton("🏠 Menu Dashboard", callback_data="nav:home")],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    elif action == "scan_dedup_topic":
        topic_id = int(parts[3]) if len(parts) > 3 else 0
        topics = udata.get("clean_available_topics") or []
        topic_name = next((t.title for t in topics if t.id == topic_id), f"Topic #{topic_id}")
        await query.edit_message_text(
            f"🔍 *Scanning topic '🧵 {topic_name}' for duplicates...*\n\nPlease wait...",
            parse_mode="Markdown",
        )
        try:
            result = await chat_cleaner_service.scan_duplicates(
                client=client, chat_id=chat_id, chat_title=chat_title, limit=500, topic_id=topic_id
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Unable to scan topic: {e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]),
            )
            return

        if not result.duplicate_ids:
            text = (
                "✅ *No Duplicates Found!*\n\n"
                f"Scanned {result.total_scanned} messages in topic **{topic_name}**.\n"
                "All media files and messages in this topic are unique."
            )
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⬅️ Back to Topic", callback_data=f"clean:topic_menu:{chat_id}:{topic_id}")],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]
            )
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
            return

        mb_saved = result.total_duplicate_bytes / (1024 * 1024)
        session_store.update_data(
            user_id,
            pending_delete_ids=result.duplicate_ids,
            clean_chat_id=chat_id,
            clean_chat_title=chat_title,
            clean_topic_id=topic_id,
            clean_topic_title=topic_name,
        )

        text = (
            "🧹 *Duplicate Scan Complete (Topic)*\n\n"
            f"• *Chat:* {chat_title}\n"
            f"• *Topic:* 🧵 {topic_name}\n"
            f"• *Scanned Messages:* {result.total_scanned}\n"
            f"• *Duplicates Found:* **{len(result.duplicate_ids)}**\n"
            f"  - Duplicate Media: {result.duplicate_media_count} (~{mb_saved:.1f} MB)\n"
            f"  - Duplicate Text: {result.duplicate_text_count}\n\n"
            f"⚠️ *Keep original copies and delete the {len(result.duplicate_ids)} redundant duplicates in this topic?*"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🗑️ Delete {len(result.duplicate_ids)} Duplicates",
                        callback_data=f"clean:exec_dedup_topic:{chat_id}:{topic_id}",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"clean:topic_menu:{chat_id}:{topic_id}")],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    elif action == "exec_dedup_topic":
        topic_id = int(parts[3]) if len(parts) > 3 else 0
        topic_name = udata.get("clean_topic_title") or f"Topic #{topic_id}"
        pending_ids = udata.get("pending_delete_ids") or []
        if not pending_ids:
            await query.edit_message_text("❌ No pending duplicates found to delete.")
            return

        await query.edit_message_text(
            f"🗑️ *Deleting {len(pending_ids)} duplicates in topic '{topic_name}'...*\n\nPlease wait...",
            parse_mode="Markdown",
        )
        try:
            del_count = await chat_cleaner_service.delete_messages_batch(client, chat_id, pending_ids)
            session_store.update_data(user_id, pending_delete_ids=[])
            text = (
                f"✅ *Successfully Cleaned Topic Duplicates!*\n\n"
                f"Removed **{del_count}** duplicate message(s) from topic **{topic_name}** in *{chat_title}*."
            )
        except RuntimeError as re:
            text = f"❌ {re}"
        except Exception as e:
            text = f"❌ Failed to delete duplicates: {e}"

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⬅️ Back to Topic", callback_data=f"clean:topic_menu:{chat_id}:{topic_id}")],
                [InlineKeyboardButton("🏠 Menu Dashboard", callback_data="nav:home")],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    elif action == "prompt_media_topic":
        topic_id = int(parts[3]) if len(parts) > 3 else 0
        topics = udata.get("clean_available_topics") or []
        topic_name = next((t.title for t in topics if t.id == topic_id), f"Topic #{topic_id}")
        await query.edit_message_text(
            f"🔍 *Scanning topic '🧵 {topic_name}' for media files...*",
            parse_mode="Markdown",
        )
        try:
            media_ids, total_bytes = await chat_cleaner_service.scan_media_files(client, chat_id, limit=500, topic_id=topic_id)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to scan media: {e}")
            return

        if not media_ids:
            text = f"ℹ️ *No media files found* in topic **{topic_name}**."
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⬅️ Back to Topic", callback_data=f"clean:topic_menu:{chat_id}:{topic_id}")],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]
            )
            await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
            return

        mb_total = total_bytes / (1024 * 1024)
        session_store.update_data(
            user_id,
            pending_delete_ids=media_ids,
            clean_chat_id=chat_id,
            clean_chat_title=chat_title,
            clean_topic_id=topic_id,
            clean_topic_title=topic_name,
        )

        text = (
            "📁 *Delete All Media Files (Topic)*\n\n"
            f"• *Chat:* {chat_title}\n"
            f"• *Topic:* 🧵 {topic_name}\n"
            f"• *Media Files Found:* **{len(media_ids)}** (~{mb_total:.1f} MB)\n\n"
            f"⚠️ *Permanently delete all {len(media_ids)} media files in this topic?*\n"
            "Text messages will NOT be deleted. This cannot be undone."
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"💥 Yes, Delete {len(media_ids)} Media Files",
                        callback_data=f"clean:exec_media_topic:{chat_id}:{topic_id}",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"clean:topic_menu:{chat_id}:{topic_id}")],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    elif action == "exec_media_topic":
        topic_id = int(parts[3]) if len(parts) > 3 else 0
        topic_name = udata.get("clean_topic_title") or f"Topic #{topic_id}"
        pending_ids = udata.get("pending_delete_ids") or []
        if not pending_ids:
            await query.edit_message_text("❌ No pending media files found.")
            return

        await query.edit_message_text(
            f"🗑️ *Deleting {len(pending_ids)} media files in topic '{topic_name}'...*\n\nPlease wait...",
            parse_mode="Markdown",
        )
        try:
            del_count = await chat_cleaner_service.delete_messages_batch(client, chat_id, pending_ids)
            session_store.update_data(user_id, pending_delete_ids=[])
            text = (
                f"✅ *Media Deletion Complete!*\n\n"
                f"Deleted **{del_count}** media file(s) from topic **{topic_name}**."
            )
        except RuntimeError as re:
            text = f"❌ {re}"
        except Exception as e:
            text = f"❌ Failed to delete media: {e}"

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⬅️ Back to Topic", callback_data=f"clean:topic_menu:{chat_id}:{topic_id}")],
                [InlineKeyboardButton("🏠 Menu Dashboard", callback_data="nav:home")],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    elif action == "prompt_range_topic":
        topic_id = int(parts[3]) if len(parts) > 3 else 0
        topics = udata.get("clean_available_topics") or []
        topic_name = next((t.title for t in topics if t.id == topic_id), f"Topic #{topic_id}")
        session_store.set_state(user_id, BotState.CLEAN_RANGE_INPUT)
        session_store.update_data(
            user_id,
            clean_chat_id=chat_id,
            clean_chat_title=chat_title,
            clean_topic_id=topic_id,
            clean_topic_title=topic_name,
        )
        text = (
            "💥 *Purge Messages by Range in Topic*\n\n"
            f"Target Chat: *{chat_title}*\n"
            f"🧵 Topic: *{topic_name}*\n\n"
            "Please enter the start and end message ID to delete:\n\n"
            "Examples:\n"
            "• `1 50` → deletes messages from ID 1 to 50\n"
            "• `100 250` → deletes messages 100 to 250\n\n"
            "Send /cancel to abort."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"clean:topic_menu:{chat_id}:{topic_id}")]])
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    # 8. Reopen clean menu
    elif action == "menu":
        await show_clean_menu(query, user_id, chat_id, chat_title)
        return


