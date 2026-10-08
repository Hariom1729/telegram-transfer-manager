"""Reusable Telegram-Native Chat/Channel Picker UI for Sources and Destinations."""

import logging
from typing import List, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from app.bot.states import BotState, session_store
from app.telegram.discovery import ChatDiscovery, DiscoveredChat
from app.telegram.topics import TopicManager
from app.telegram.user_client import user_client_manager

logger = logging.getLogger(__name__)

PAGE_SIZE = 5


class ChatPicker:
    """Unified and reusable UI component for selecting source or destination Telegram dialogs."""

    # -------------------------------------------------------------------------
    # Keyboards
    # -------------------------------------------------------------------------

    @staticmethod
    def build_menu_keyboard(target: str) -> InlineKeyboardMarkup:
        """Build main picker menu with search, categories, recent, and refresh."""
        code = "src" if target in ("source", "src") else "dst"
        prefix = f"cp:{code}"
        back_btn = (
            InlineKeyboardButton("🏠 Home", callback_data="nav:home")
            if code == "src"
            else InlineKeyboardButton("⬅️ Back to Source", callback_data="cp:src:menu")
        )
        keyboard = [
            [
                InlineKeyboardButton("🔎 Search", callback_data=f"{prefix}:search"),
            ],
            [
                InlineKeyboardButton("📢 Channels", callback_data=f"{prefix}:cat:channel"),
                InlineKeyboardButton("👥 Groups", callback_data=f"{prefix}:cat:group"),
            ],
            [
                InlineKeyboardButton("💬 Private", callback_data=f"{prefix}:cat:private"),
                InlineKeyboardButton("📋 All", callback_data=f"{prefix}:cat:all"),
            ],
            [
                InlineKeyboardButton("📋 Recent", callback_data=f"{prefix}:recent"),
                InlineKeyboardButton("🔄 Refresh Chats", callback_data=f"{prefix}:refresh"),
            ],
            [
                back_btn,
                InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def build_list_keyboard(
        chats: List[DiscoveredChat],
        target: str,
        page: int = 0,
        page_size: int = PAGE_SIZE,
        current_view: str = "cat",  # 'cat', 'search', or 'recent'
    ) -> InlineKeyboardMarkup:
        """Build paginated chat list with compact IDs in callback_data."""
        code = "src" if target in ("source", "src") else "dst"
        prefix = f"cp:{code}"
        total_chats = len(chats)
        total_pages = max(1, (total_chats + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))

        start_idx = page * page_size
        page_items = chats[start_idx : start_idx + page_size]

        buttons = []
        for chat in page_items:
            # Compact callback_data: cp:src:pk:-1001234567890 (well within 64 byte limit)
            title_display = chat.title[:24] + ("…" if len(chat.title) > 24 else "")
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{chat.display_icon} {title_display}",
                        callback_data=f"{prefix}:pk:{chat.id}",
                    )
                ]
            )

        # Pagination controls
        if total_pages > 1:
            nav_row = []
            if page > 0:
                nav_row.append(
                    InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:pg:{page - 1}")
                )
            nav_row.append(
                InlineKeyboardButton(f"[{page + 1}/{total_pages}]", callback_data="noop")
            )
            if page < total_pages - 1:
                nav_row.append(
                    InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:pg:{page + 1}")
                )
            buttons.append(nav_row)

        # Context action row
        action_row = []
        if current_view == "search":
            action_row.append(
                InlineKeyboardButton("🔎 New Search", callback_data=f"{prefix}:search")
            )
        action_row.append(
            InlineKeyboardButton("🔄 Refresh Chats", callback_data=f"{prefix}:refresh")
        )
        buttons.append(action_row)

        # Back and Cancel
        buttons.append(
            [
                InlineKeyboardButton("⬅️ Back", callback_data=f"{prefix}:menu"),
                InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
            ]
        )
        return InlineKeyboardMarkup(buttons)

    @staticmethod
    def build_error_keyboard(target: str) -> InlineKeyboardMarkup:
        """Build error keyboard with retry and accounts shortcuts."""
        keyboard = [
            [
                InlineKeyboardButton("🔄 Retry", callback_data=f"cp:retry:{target}"),
                InlineKeyboardButton("🔗 Connected Accounts", callback_data="nav:accounts"),
            ],
            [
                InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    # -------------------------------------------------------------------------
    # Core Picker Presentation Methods
    # -------------------------------------------------------------------------

    @classmethod
    async def show_source_picker(cls, query_or_message, user_id: int) -> None:
        """Display the initial Source chat selection menu."""
        await cls.show_picker(query_or_message, user_id, target="source")

    @classmethod
    async def show_destination_picker(cls, query_or_message, user_id: int) -> None:
        """Display the initial Destination chat selection menu."""
        await cls.show_picker(query_or_message, user_id, target="dest")

    @classmethod
    async def show_picker(
        cls, query_or_message, user_id: int, target: str
    ) -> None:
        """Show main picker menu for source or destination."""
        state = (
            BotState.WIZARD_SOURCE_SELECT
            if target == "source"
            else BotState.WIZARD_DEST_SELECT
        )
        session_store.set_state(user_id, state)
        session_store.update_data(
            user_id,
            cp_target=target,
            cp_view="menu",
            cp_page=0,
            cp_query="",
        )

        label = "📥 *Select Source*" if target == "source" else "📤 *Select Destination*"
        text = (
            f"{label}\n\n"
            "Search or choose a category from your connected Telegram account:"
        )

        kb = cls.build_menu_keyboard(target)
        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
        else:
            await query_or_message.reply_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )

    @classmethod
    async def show_search_prompt(
        cls, query_or_message, user_id: int, target: str
    ) -> None:
        """Prompt the user to enter search keywords."""
        state = (
            BotState.WIZARD_SOURCE_INPUT
            if target == "source"
            else BotState.WIZARD_DEST_INPUT
        )
        session_store.set_state(user_id, state)
        session_store.update_data(user_id, cp_target=target, cp_view="search_input")

        label = "Source" if target == "source" else "Destination"
        text = (
            f"🔎 *Search {label} Chats*\n\n"
            "Enter any search text (channel title, @username, or keywords).\n\n"
            "💡 *Tip:* You do not need to type the full name.\n"
            "Examples:\n"
            "• `dr` → finds Driver Updates, Driver Tutorials\n"
            "• `news` → finds all news channels\n"
            "• `-1001234567890` → finds specific chat by ID"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=f"cp:{target}:menu")]]
        )
        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
        else:
            await query_or_message.reply_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )

    @classmethod
    async def handle_search_query(
        cls,
        message_or_query,
        user_id: int,
        target: str,
        query_text: str,
        page: int = 0,
    ) -> None:
        """Search dialogs with ranking and display paginated results."""
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")
        client = await user_client_manager.get_client_for_account(account_id)

        if not client:
            await cls._show_error(
                message_or_query,
                target,
                "Telegram account session is disconnected.",
            )
            return

        try:
            # Ensure dialogs are cached
            await ChatDiscovery.load_dialogs(account_id, client)
            results = await ChatDiscovery.search_dialogs(
                account_id=account_id,
                query=query_text,
                client=client,
            )
        except FloodWaitError as e:
            await cls._show_error(
                message_or_query,
                target,
                f"Telegram rate limit (FloodWait {e.seconds}s).",
            )
            return
        except Exception as e:
            logger.error("Failed to search dialogs: %s", e)
            await cls._show_error(message_or_query, target)
            return

        session_store.update_data(
            user_id,
            cp_target=target,
            cp_view="search",
            cp_query=query_text,
            cp_page=page,
            cp_active_chats=results,
        )

        label = "Source" if target == "source" else "Destination"
        clean_q = query_text.strip()

        if not results:
            text = (
                f"🔎 *Search {label} Results for:* `{clean_q}`\n\n"
                "No accessible chats found matching your search.\n\n"
                "Try a shorter query, check spelling, or browse by category:"
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔎 Try Another Search",
                            callback_data=f"cp:{target}:search",
                        ),
                        InlineKeyboardButton(
                            "📋 All Chats", callback_data=f"cp:{target}:cat:all"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🔄 Refresh Chats", callback_data=f"cp:{target}:refresh"
                        ),
                        InlineKeyboardButton("⬅️ Back", callback_data=f"cp:{target}:menu"),
                    ],
                ]
            )
        else:
            text = (
                f"🔎 *Search {label} Results for:* `{clean_q}`\n"
                f"Found {len(results)} matching chats:\n\n"
                "Tap a chat to select it:"
            )
            kb = cls.build_list_keyboard(
                chats=results,
                target=target,
                page=page,
                current_view="search",
            )

        if hasattr(message_or_query, "edit_message_text"):
            await message_or_query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
        else:
            await message_or_query.reply_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )

    @classmethod
    async def show_category(
        cls,
        query_or_message,
        user_id: int,
        target: str,
        category: str,
        page: int = 0,
    ) -> None:
        """Display dialogs filtered by category (channel, group, private, all)."""
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")
        client = await user_client_manager.get_client_for_account(account_id)

        if not client:
            await cls._show_error(
                query_or_message,
                target,
                "Telegram account session is disconnected.",
            )
            return

        try:
            await ChatDiscovery.load_dialogs(account_id, client)
            results = await ChatDiscovery.search_dialogs(
                account_id=account_id,
                query="",
                chat_type=category,
                client=client,
            )
        except FloodWaitError as e:
            await cls._show_error(
                query_or_message,
                target,
                f"Telegram rate limit (FloodWait {e.seconds}s).",
            )
            return
        except Exception as e:
            logger.error("Failed to load category %s dialogs: %s", category, e)
            await cls._show_error(query_or_message, target)
            return

        session_store.update_data(
            user_id,
            cp_target=target,
            cp_view="cat",
            cp_category=category,
            cp_page=page,
            cp_active_chats=results,
        )

        cat_names = {
            "channel": "📢 Channels",
            "group": "👥 Groups",
            "private": "💬 Private Chats",
            "all": "📋 All Chats",
        }
        title = cat_names.get(category, "Chats")
        label = "Source" if target == "source" else "Destination"

        if not results:
            text = (
                f"{title} ({label})\n\n"
                f"No accessible {category}s found in your Telegram account."
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Refresh Chats", callback_data=f"cp:{target}:refresh"
                        ),
                        InlineKeyboardButton("⬅️ Back", callback_data=f"cp:{target}:menu"),
                    ]
                ]
            )
        else:
            text = (
                f"{title} — *Select {label}* ({len(results)})\n\n"
                "Tap a chat to select it:"
            )
            kb = cls.build_list_keyboard(
                chats=results,
                target=target,
                page=page,
                current_view="cat",
            )

        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
        else:
            await query_or_message.reply_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )

    @classmethod
    async def show_recent_chats(
        cls,
        query_or_message,
        user_id: int,
        target: str,
        page: int = 0,
    ) -> None:
        """Display recently selected chats from database."""
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")

        recents = await ChatDiscovery.get_recent_dialogs(account_id, limit=20)
        session_store.update_data(
            user_id,
            cp_target=target,
            cp_view="recent",
            cp_page=page,
            cp_active_chats=recents,
        )

        label = "Sources" if target == "source" else "Destinations"
        if not recents:
            text = (
                f"📋 *Recent {label}*\n\n"
                "No recently used chats recorded yet.\n"
                "Select a chat using Search or Categories above."
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔎 Search", callback_data=f"cp:{target}:search"
                        ),
                        InlineKeyboardButton(
                            "📋 All Chats", callback_data=f"cp:{target}:cat:all"
                        ),
                    ],
                    [InlineKeyboardButton("⬅️ Back", callback_data=f"cp:{target}:menu")],
                ]
            )
        else:
            text = (
                f"📋 *Recent {label}*\n\n"
                "Choose from your recently selected chats:"
            )
            kb = cls.build_list_keyboard(
                chats=recents,
                target=target,
                page=page,
                current_view="recent",
            )

        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
        else:
            await query_or_message.reply_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )

    @classmethod
    async def refresh_dialogs(cls, query, user_id: int, target: str) -> None:
        """Refresh dialogs from Telethon and update current view."""
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")
        client = await user_client_manager.get_client_for_account(account_id)

        if not client:
            await cls._show_error(
                query, target, "Telegram account session is disconnected."
            )
            return

        try:
            await query.answer("🔄 Refreshing chats from Telegram...")
            await ChatDiscovery.refresh_dialogs(account_id, client)
        except FloodWaitError as e:
            await query.answer(
                f"⏳ Rate limited by Telegram: wait {e.seconds}s",
                show_alert=True,
            )
            return
        except Exception as e:
            logger.error("Failed to refresh dialogs: %s", e)
            await cls._show_error(query, target)
            return

        # Restore current view
        view = udata.get("cp_view", "menu")
        page = udata.get("cp_page", 0)

        if view == "search":
            q = udata.get("cp_query", "")
            await cls.handle_search_query(query, user_id, target, q, page=page)
        elif view == "cat":
            cat = udata.get("cp_category", "all")
            await cls.show_category(query, user_id, target, cat, page=page)
        elif view == "recent":
            await cls.show_recent_chats(query, user_id, target, page=page)
        else:
            await cls.show_picker(query, user_id, target)

    @classmethod
    async def handle_pick(
        cls, query, user_id: int, target: str, chat_id: int
    ) -> None:
        """Handle selection of a chat by chat_id for source or destination."""
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")
        active_chats = udata.get("cp_active_chats", [])

        # Find chat info in active list or cache
        picked: Optional[DiscoveredChat] = next(
            (c for c in active_chats if c.id == chat_id), None
        )
        if not picked:
            cached = ChatDiscovery._dialog_cache.get(account_id, [])
            picked = next((c for c in cached if c.id == chat_id), None)

        if not picked:
            # Fallback representation
            picked = DiscoveredChat(
                id=chat_id,
                title=str(chat_id),
                chat_type="chat",
            )

        # Record recent selection in database
        await ChatDiscovery.record_chat_selection(account_id, picked)

        if target == "source":
            session_store.update_data(
                user_id,
                source_chat_id=picked.id,
                source_chat_title=picked.title,
            )
            text = (
                f"✅ *Source Selected*\n\n"
                f"{picked.display_icon} *{picked.title}*\n"
                f"`ID: {picked.id}`\n\n"
                "Next step: Select the transfer destination."
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "➡️ Select Destination",
                            callback_data="cp:src:to_dest",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Change Source", callback_data="cp:src:menu"
                        ),
                        InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
                    ],
                ]
            )
            await query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )

        elif target == "dest":
            session_store.update_data(
                user_id,
                destination_chat_id=picked.id,
                destination_chat_title=picked.title,
                is_forum=picked.is_forum,
            )

            client = await user_client_manager.get_client_for_account(account_id)

            # Check if destination is a forum supergroup
            if picked.is_forum and client:
                try:
                    topics = await TopicManager.get_topics(client, picked.id)
                    session_store.update_data(user_id, available_topics=topics)
                    text = (
                        f"✅ *Destination Selected*\n\n"
                        f"{picked.display_icon} *{picked.title}*\n\n"
                        "🧵 *This group has Topics enabled.*\n"
                        "Select a destination topic:"
                    )
                    # Inline topic keyboard
                    from app.bot.keyboards import build_topics_keyboard

                    await query.edit_message_text(
                        text=text,
                        reply_markup=build_topics_keyboard(
                            topics, can_create=picked.can_manage_topics
                        ),
                        parse_mode="Markdown",
                    )
                    return
                except Exception as e:
                    logger.error("Failed to retrieve forum topics: %s", e)

            # If not a forum, continue straight to content filter settings
            from app.bot.callbacks import _show_content_filter

            await _show_content_filter(query, user_id)

    @classmethod
    async def _show_error(
        cls, query_or_message, target: str, reason: Optional[str] = None
    ) -> None:
        """Display error screen matching Section 17 specification."""
        text = (
            "❌ *Unable to load your Telegram chats.*\n\n"
            "Possible reasons:\n"
            "• Telegram account is disconnected\n"
            "• Session expired\n"
            "• Network error\n"
            "• Telegram temporarily rate-limited the request\n"
        )
        if reason:
            text += f"\n_Details: {reason}_"

        kb = cls.build_error_keyboard(target)
        if hasattr(query_or_message, "edit_message_text"):
            await query_or_message.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
        else:
            await query_or_message.reply_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
