"""Message Browser module for browsing chat message history and selecting ranges."""

from __future__ import annotations

import logging
from typing import Any, List, Optional
from telethon.tl.types import InputMessagesFilterVideo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from app.bot.states import BotState, session_store
from app.telegram.user_client import user_client_manager
from app.transfer.copier import MessageCopier

logger = logging.getLogger(__name__)


def _escape_md(text: str) -> str:
    """Escape Markdown v1 special characters in dynamic strings."""
    if not text:
        return ""
    # Characters that trigger Telegram markdown formatting
    for ch in ("*", "_", "`", "[", "]"):
        text = text.replace(ch, "\\" + ch)
    return text


class MessageBrowser:
    """Handles paginated message browsing, video filtering, search, and jump."""

    PAGE_SIZE = 25

    @classmethod
    async def show_browser(
        cls,
        update_or_query: Any,
        user_id: int,
        reset: bool = False,
        jump_id: Optional[int] = None,
        search_query: Optional[str] = None,
    ) -> None:
        """Display messages per page (25 items) with navigation, filters, and sort order."""
        udata = session_store.get_data(user_id)
        source_id = udata.get("source_chat_id")
        source_title = udata.get("source_chat_title", "Source Chat")
        source_thread_id = udata.get("source_thread_id")
        source_topic_name = udata.get("source_topic_name")
        account_id = udata.get("account_id")

        if not source_id:
            text = "❌ *No source chat selected.*\nPlease select a source chat first."
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Select Source", callback_data="cp:src:menu")]]
            )
            await cls._render(update_or_query, text, kb)
            return

        client = await user_client_manager.get_active_client(account_id)
        if not client or not client.is_connected():
            text = "❌ *Telegram account session is inactive or disconnected.*"
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="nav:home")]]
            )
            await cls._render(update_or_query, text, kb)
            return

        # Pagination, Filter & Sort Order state initialization (Default: "asc" = increasing / oldest first)
        order = str(udata.get("browse_order", "asc"))
        if reset or "browse_offset_stack" not in udata:
            offset_stack: List[int] = [0]
            offset_id = 0
            page = 0
            video_only = bool(udata.get("browse_video_only", False))
            search = None
        else:
            offset_stack = list(udata.get("browse_offset_stack", [0]))
            offset_id = int(udata.get("browse_offset_id", 0))
            page = int(udata.get("browse_page", 0))
            video_only = bool(udata.get("browse_video_only", False))
            search = udata.get("browse_search_query")

        if jump_id is not None:
            # Jumping to an ID:
            # In asc order: offset_id = max(0, jump_id - 1) causes Telethon (with reverse=True)
            # to start right at jump_id onwards in increasing order.
            # In desc order: offset_id = jump_id + 1 fetches messages <= jump_id downwards.
            if order == "asc":
                offset_id = max(0, jump_id - 1)
            else:
                offset_id = max(1, jump_id + 1)
            offset_stack = [offset_id]
            page = 0
            search = None

        if search_query is not None:
            clean_search = search_query.strip()
            search = clean_search if clean_search else None
            offset_stack = [0]
            offset_id = 0
            page = 0

        # Fetch messages via Telethon MTProto APIs (metadata only, no media downloaded)
        filter_obj = InputMessagesFilterVideo() if video_only else None
        messages: List[Any] = []
        is_reverse = (order == "asc")

        try:
            source_entity = await client.get_entity(source_id)
            get_kwargs: dict[str, Any] = {
                "limit": cls.PAGE_SIZE,
                "offset_id": offset_id,
                "reverse": is_reverse,
            }
            if filter_obj is not None:
                get_kwargs["filter"] = filter_obj
            if search:
                get_kwargs["search"] = search
            if source_thread_id:
                get_kwargs["reply_to"] = source_thread_id

            fetched = await client.get_messages(source_entity, **get_kwargs)
            messages = list(fetched) if fetched else []
        except Exception as e:
            logger.warning(
                "Telethon get_messages failed with filter: %s. Attempting fallback...", e
            )
            try:
                source_entity = await client.get_entity(source_id)
                fallback_kwargs: dict[str, Any] = {
                    "limit": cls.PAGE_SIZE if not video_only else (cls.PAGE_SIZE * 3),
                    "offset_id": offset_id,
                    "reverse": is_reverse,
                }
                if search:
                    fallback_kwargs["search"] = search
                if source_thread_id:
                    fallback_kwargs["reply_to"] = source_thread_id

                fetched_fallback = await client.get_messages(
                    source_entity, **fallback_kwargs
                )
                if video_only and fetched_fallback:
                    messages = [
                        m
                        for m in fetched_fallback
                        if MessageCopier.classify_message_content(m) == "video"
                    ][: cls.PAGE_SIZE]
                else:
                    messages = list(fetched_fallback) if fetched_fallback else []
            except Exception as fe:
                logger.error("Failed to fetch messages for browser: %s", fe)
                text = (
                    f"❌ *Could not fetch messages from source chat.*\n\n"
                    f"Telegram returned: `{_escape_md(str(fe))}`"
                )
                kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data="browse:back")]]
                )
                await cls._render(update_or_query, text, kb)
                return

        valid_messages = [
            m for m in messages if m and getattr(m, "id", None) and not getattr(m, "empty", False)
        ]

        # Ensure correct ordering on screen
        if order == "asc":
            valid_messages.sort(key=lambda m: m.id)
            advance_id = max(m.id for m in valid_messages) if valid_messages else offset_id
        else:
            valid_messages.sort(key=lambda m: m.id, reverse=True)
            advance_id = min(m.id for m in valid_messages) if valid_messages else offset_id

        # Update session store
        session_store.update_data(
            user_id,
            browse_page=page,
            browse_offset_id=offset_id,
            browse_offset_stack=offset_stack,
            browse_video_only=video_only,
            browse_search_query=search,
            browse_order=order,
            browse_last_count=len(valid_messages),
            browse_last_advance_id=advance_id,
            browse_last_oldest_id=advance_id,
        )
        session_store.set_state(user_id, BotState.WIZARD_BROWSE_MESSAGES)

        # Header
        lines: List[str] = []
        topic_disp = (
            f" • 🧵 `{_escape_md(source_topic_name)}`" if source_topic_name else ""
        )
        order_badge = "⬆️ Oldest First" if order == "asc" else "⬇️ Newest First"
        lines.append(f"📂 *Browse Messages — Page {page + 1}* ({order_badge})")
        lines.append(f"📢 *{_escape_md(source_title)}*{topic_disp}")

        status_chips: List[str] = []
        if video_only:
            status_chips.append("📹 Video Only: ON")
        if search:
            status_chips.append(f"🔍 '{_escape_md(search)}'")
        status_chips.append(f"{len(valid_messages)} items")
        lines.append("⚙️ _" + " | ".join(status_chips) + "_")
        lines.append("")

        if not valid_messages:
            lines.append("⚠️ _No accessible messages found on this page._\n")
            if video_only:
                lines.append("ℹ️ _Try disabling the Video Only filter to view all content._\n")
        else:
            for m in valid_messages:
                m_type = MessageCopier.classify_message_content(m)

                # Media type icon
                if m_type == "video":
                    icon_type = "📹 [VIDEO]"
                elif m_type == "photo":
                    icon_type = "🖼 Photo"
                elif m_type == "document":
                    icon_type = "📄 Doc"
                elif m_type == "audio":
                    icon_type = "🎵 Audio"
                elif m_type == "animation":
                    icon_type = "🎞 GIF"
                elif m_type == "poll":
                    icon_type = "📊 Poll"
                elif m_type == "text":
                    icon_type = "💬 Text"
                else:
                    icon_type = "📁 Media"

                # Check filename when available
                filename: Optional[str] = None
                if getattr(m, "file", None) and getattr(m.file, "name", None):
                    filename = m.file.name
                elif hasattr(m, "document") and m.document:
                    for attr in getattr(m.document, "attributes", []):
                        if hasattr(attr, "file_name") and attr.file_name:
                            filename = attr.file_name
                            break

                # Compact filename display
                name_disp = ""
                if filename:
                    clean_fn = filename.replace("`", "")
                    if len(clean_fn) > 34:
                        clean_fn = clean_fn[:31] + "..."
                    name_disp = f" `{clean_fn}`"

                # Date
                date_str = (
                    f" | 📅 `{m.date.strftime('%m-%d %H:%M')}`"
                    if getattr(m, "date", None)
                    else ""
                )

                # Caption preview (omit if duplicates filename or empty)
                caption_line = ""
                raw_text = (m.message or "").strip()
                if raw_text:
                    clean_text = " ".join(raw_text.split())
                    is_dup = False
                    if filename:
                        fn_clean = filename.lower().replace("_", " ").replace("-", " ")
                        ct_clean = clean_text.lower().replace("_", " ").replace("-", " ")
                        if fn_clean in ct_clean or ct_clean in fn_clean:
                            is_dup = True
                    if not is_dup:
                        if len(clean_text) > 40:
                            clean_text = clean_text[:37] + "..."
                        caption_line = f"\n   💬 _{_escape_md(clean_text)}_"

                lines.append(f"🆔 *#{m.id}* • *{icon_type}*{name_disp}{date_str}{caption_line}")

        text_content = "\n".join(lines)
        if len(text_content) > 4000:
            text_content = (
                text_content[:3950].rsplit("\n", 1)[0]
                + "\n\n⚠️ _(Page truncated to fit Telegram message limit)_"
            )

        # Build Navigation Keyboard
        buttons: List[List[InlineKeyboardButton]] = []

        # Row 1: Pagination & Refresh
        nav_row: List[InlineKeyboardButton] = []
        if len(offset_stack) > 1 or page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data="browse:prev"))
        nav_row.append(InlineKeyboardButton("🔄 Refresh", callback_data="browse:refresh"))
        if len(valid_messages) >= cls.PAGE_SIZE:
            nav_row.append(InlineKeyboardButton("Next ▶️", callback_data="browse:next"))
        buttons.append(nav_row)

        # Row 2: Video Filter & Sort Order Toggle
        vid_label = "🎬 Video: ON" if video_only else "🎬 Video: OFF"
        order_label = "⬆️ Oldest First" if order == "asc" else "⬇️ Newest First"
        buttons.append(
            [
                InlineKeyboardButton(vid_label, callback_data="browse:toggle_video"),
                InlineKeyboardButton(order_label, callback_data="browse:toggle_order"),
            ]
        )

        # Row 3: Filters & Jump
        search_btn = (
            InlineKeyboardButton("❌ Clear Search", callback_data="browse:clear_search")
            if search
            else InlineKeyboardButton("🔍 Search", callback_data="browse:search")
        )
        buttons.append(
            [
                search_btn,
                InlineKeyboardButton("🔢 Jump to ID", callback_data="browse:jump"),
            ]
        )

        # Row 4: Range & Destination/Continue
        dest_id = udata.get("destination_chat_id")
        cont_label = "➡️ Continue to Transfer" if dest_id is not None else "➡️ Select Destination"
        buttons.append(
            [
                InlineKeyboardButton("🎯 Select Range", callback_data="browse:select_range"),
                InlineKeyboardButton(cont_label, callback_data="browse:to_dest"),
            ]
        )

        # Row 5: Back & Home
        buttons.append(
            [
                InlineKeyboardButton("⬅️ Back to Source Menu", callback_data="browse:back"),
                InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            ]
        )

        kb = InlineKeyboardMarkup(buttons)
        await cls._render(update_or_query, text_content, kb)

    @classmethod
    async def handle_next(cls, query: Any, user_id: int) -> None:
        """Navigate to next page."""
        udata = session_store.get_data(user_id)
        advance_id = udata.get("browse_last_advance_id") or udata.get("browse_last_oldest_id")
        offset_stack = list(udata.get("browse_offset_stack", [0]))
        page = int(udata.get("browse_page", 0))
        order = udata.get("browse_order", "asc")

        if not advance_id or udata.get("browse_last_count", 0) < cls.PAGE_SIZE:
            msg = "Reached newest messages." if order == "asc" else "Reached oldest messages."
            await query.answer(msg, show_alert=False)
            return

        offset_stack.append(advance_id)
        session_store.update_data(
            user_id,
            browse_offset_id=advance_id,
            browse_offset_stack=offset_stack,
            browse_page=page + 1,
        )
        await query.answer()
        await cls.show_browser(query, user_id, reset=False)

    @classmethod
    async def handle_prev(cls, query: Any, user_id: int) -> None:
        """Navigate to previous page."""
        udata = session_store.get_data(user_id)
        offset_stack = list(udata.get("browse_offset_stack", [0]))
        page = int(udata.get("browse_page", 0))

        if len(offset_stack) > 1:
            offset_stack.pop()
            new_offset = offset_stack[-1]
            new_page = max(0, page - 1)
        else:
            new_offset = 0
            new_page = 0
            offset_stack = [0]

        session_store.update_data(
            user_id,
            browse_offset_id=new_offset,
            browse_offset_stack=offset_stack,
            browse_page=new_page,
        )
        await query.answer()
        await cls.show_browser(query, user_id, reset=False)

    @classmethod
    async def toggle_order(cls, query: Any, user_id: int) -> None:
        """Toggle between ascending (oldest first) and descending (newest first)."""
        udata = session_store.get_data(user_id)
        current_order = udata.get("browse_order", "asc")
        new_order = "desc" if current_order == "asc" else "asc"
        session_store.update_data(
            user_id,
            browse_order=new_order,
            browse_page=0,
            browse_offset_id=0,
            browse_offset_stack=[0],
        )
        desc = "Oldest First (Ascending ⬆️)" if new_order == "asc" else "Newest First (Descending ⬇️)"
        await query.answer(f"Order: {desc}")
        await cls.show_browser(query, user_id, reset=False)

    @classmethod
    async def toggle_video(cls, query: Any, user_id: int) -> None:
        """Toggle video-only filter on/off."""
        udata = session_store.get_data(user_id)
        new_val = not bool(udata.get("browse_video_only", False))
        session_store.update_data(
            user_id,
            browse_video_only=new_val,
            browse_page=0,
            browse_offset_id=0,
            browse_offset_stack=[0],
            content_types=["video"] if new_val else ["all"],
        )
        status_txt = "🎬 Video: ON" if new_val else "📦 Video: OFF (All content)"
        await query.answer(status_txt)
        await cls.show_browser(query, user_id, reset=False)

    @classmethod
    async def clear_search(cls, query: Any, user_id: int) -> None:
        """Clear search query filter."""
        session_store.update_data(
            user_id,
            browse_search_query=None,
            browse_page=0,
            browse_offset_id=0,
            browse_offset_stack=[0],
        )
        await query.answer("Search cleared.")
        await cls.show_browser(query, user_id, reset=False)

    @classmethod
    async def _render(
        cls,
        update_or_query: Any,
        text: str,
        reply_markup: InlineKeyboardMarkup,
    ) -> None:
        """Render markdown text with reply markup."""
        if hasattr(update_or_query, "edit_message_text"):
            try:
                await update_or_query.edit_message_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown",
                )
                return
            except Exception as e:
                logger.debug("edit_message_text failed, falling back to message.reply_text: %s", e)

        if hasattr(update_or_query, "reply_text"):
            await update_or_query.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        elif hasattr(update_or_query, "message") and hasattr(
            update_or_query.message, "reply_text"
        ):
            await update_or_query.message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
