from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, List, Optional, Tuple, Union
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from app.bot.states import BotState, session_store
from app.telegram.discovery import ChatDiscovery, DiscoveredChat
from app.telegram.topics import TopicManager
from app.telegram.user_client import user_client_manager

logger = logging.getLogger(__name__)

PAGE_SIZE: int = 15
RESULTS_PER_PAGE: int = 15


class ChatPicker:
    """Unified and reusable UI component for selecting source or destination Telegram dialogs."""

    # -------------------------------------------------------------------------
    # Target-isolated State Helpers (Source vs Destination)
    # -------------------------------------------------------------------------

    @classmethod
    def _normalize_target(cls, target: str) -> str:
        return "src" if target in ("source", "src") else "dst"

    @classmethod
    def get_target_state(cls, user_id: int, target: str) -> dict:
        """Retrieve isolated state for either source or destination selection."""
        code = cls._normalize_target(target)
        udata = session_store.get_data(user_id)
        return {
            "query": udata.get(f"cp_{code}_query", ""),
            "page": udata.get(f"cp_{code}_page", 0),
            "view": udata.get(f"cp_{code}_view", "menu"),
            "category": udata.get(f"cp_{code}_category", "all"),
            "chats": udata.get(f"cp_{code}_chats", []),
            "prompt_msg_id": udata.get(f"cp_{code}_prompt_msg_id"),
        }

    @classmethod
    def update_target_state(cls, user_id: int, target: str, **kwargs) -> None:
        """Update isolated state for either source or destination selection."""
        code = cls._normalize_target(target)
        updates = {}
        for k, v in kwargs.items():
            updates[f"cp_{code}_{k}"] = v
        # Backward compatibility with legacy session keys
        if "query" in kwargs:
            updates["cp_query"] = kwargs["query"]
        if "page" in kwargs:
            updates["cp_page"] = kwargs["page"]
        if "view" in kwargs:
            updates["cp_view"] = kwargs["view"]
        if "category" in kwargs:
            updates["cp_category"] = kwargs["category"]
        if "chats" in kwargs:
            updates["cp_active_chats"] = kwargs["chats"]
        if "prompt_msg_id" in kwargs:
            updates["cp_prompt_msg_id"] = kwargs["prompt_msg_id"]
        session_store.update_data(user_id, **updates)

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
        ]
        if code == "dst":
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "💾 Save to Downloads Folder",
                        callback_data="cp:dst:pk:0",
                    )
                ]
            )
        keyboard.append(
            [
                back_btn,
                InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
            ]
        )
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
            title_display = chat.title[:26] + ("…" if len(chat.title) > 26 else "")
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
                    InlineKeyboardButton("⬅️ Previous", callback_data=f"{prefix}:pg:{page - 1}")
                )
            nav_row.append(
                InlineKeyboardButton(f"Page {page + 1}/{total_pages}", callback_data="noop")
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
            if target in ("source", "src")
            else BotState.WIZARD_DEST_INPUT
        )
        session_store.set_state(user_id, state)

        code = cls._normalize_target(target)
        label = "Source" if code == "src" else "Destination"
        text = (
            f"🔎 *Search {label} Chats*\n\n"
            "Type at least 1 character to search chats.\n\n"
            "💡 *Tip:* Results update dynamically as you type.\n"
            "Examples:\n"
            "• `a` → all chats matching 'a'\n"
            "• `c` → courses, coding groups\n"
            "• `py` → Python channels\n"
            "• `-1001234567890` → chat by ID"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=f"cp:{code}:menu")]]
        )
        prompt_msg_id = None
        if hasattr(query_or_message, "edit_message_text"):
            msg = await query_or_message.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
            prompt_msg_id = getattr(msg, "message_id", None) or getattr(
                getattr(query_or_message, "message", None), "message_id", None
            )
        else:
            msg = await query_or_message.reply_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
            prompt_msg_id = getattr(msg, "message_id", None)

        cls.update_target_state(
            user_id,
            target,
            view="search_input",
            prompt_msg_id=prompt_msg_id,
        )

    @classmethod
    async def handle_search_query(
        cls,
        message_or_query,
        user_id: int,
        target: str,
        query_text: str,
        page: int = 0,
        edit_message_id: Optional[int] = None,
        bot: Optional[Any] = None,
    ) -> None:
        """Search dialogs with ranking and display paginated suggestions."""
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")
        if not account_id:
            accounts = await user_client_manager.list_user_accounts(user_id)
            if accounts:
                account_id = accounts[0].id
                session_store.update_data(user_id, account_id=account_id)

        client = (
            await user_client_manager.get_client_for_account(account_id)
            if account_id
            else None
        )

        if not client:
            await cls._show_error(
                message_or_query,
                target,
                "Telegram account session is disconnected.",
            )
            return

        is_conn = client.is_connected()
        if inspect.isawaitable(is_conn):
            is_conn = await is_conn
        if not is_conn:
            try:
                await client.connect()
            except Exception as e:
                logger.warning("Could not reconnect client for account %s: %s", account_id, e)

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

        # For destination search, only include chats the account can post to
        if target in ("dest", "dst"):
            results = [c for c in results if c.can_post]

        cls.update_target_state(
            user_id,
            target,
            view="search",
            query=query_text,
            page=page,
            chats=results,
        )

        code = cls._normalize_target(target)
        prefix = f"cp:{code}"
        label = "Source" if code == "src" else "Destination"
        clean_q = query_text.strip()

        total_chats = len(results)
        total_pages = max(1, (total_chats + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start_idx = page * PAGE_SIZE
        end_idx = min(start_idx + PAGE_SIZE, total_chats)

        if not results:
            text = (
                f"🔎 *No chats found*\n\n"
                f"Query: `{clean_q}`\n\n"
                "Try:\n"
                "• another keyword\n"
                "• shorter search term\n"
                "• username\n"
                "• refresh chats"
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Refresh Chats", callback_data=f"{prefix}:refresh"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔎 New Search", callback_data=f"{prefix}:search"
                        )
                    ],
                    [
                        InlineKeyboardButton("⬅️ Back", callback_data=f"{prefix}:menu")
                    ],
                ]
            )
        else:
            text = (
                f"🔎 *Search Results*\n\n"
                f"Query: `{clean_q}`\n"
                f"Found {total_chats} matching chats (Page {page + 1}/{total_pages})\n"
                f"Showing: {start_idx + 1}–{end_idx}\n\n"
                "Tap a chat to select it:"
            )
            kb = cls.build_list_keyboard(
                chats=results,
                target=target,
                page=page,
                page_size=PAGE_SIZE,
                current_view="search",
            )

        # In-place message edit to prevent message spam when typing
        edited = False
        chat_id = getattr(message_or_query, "chat_id", None) or getattr(
            getattr(message_or_query, "message", None), "chat_id", None
        )

        if edit_message_id and bot and chat_id:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_message_id,
                    text=text,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
                edited = True
            except Exception:
                pass

        if not edited:
            if hasattr(message_or_query, "edit_message_text"):
                try:
                    await message_or_query.edit_message_text(
                        text=text, reply_markup=kb, parse_mode="Markdown"
                    )
                except Exception:
                    pass
            else:
                sent_msg = await message_or_query.reply_text(
                    text=text, reply_markup=kb, parse_mode="Markdown"
                )
                cls.update_target_state(
                    user_id,
                    target,
                    prompt_msg_id=sent_msg.message_id,
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
        if not account_id:
            accounts = await user_client_manager.list_user_accounts(user_id)
            if accounts:
                account_id = accounts[0].id
                session_store.update_data(user_id, account_id=account_id)

        client = (
            await user_client_manager.get_client_for_account(account_id)
            if account_id
            else None
        )

        if not client:
            await cls._show_error(
                query_or_message,
                target,
                "Telegram account session is disconnected.",
            )
            return

        is_conn = client.is_connected()
        if inspect.isawaitable(is_conn):
            is_conn = await is_conn
        if not is_conn:
            try:
                await client.connect()
            except Exception as e:
                logger.warning("Could not reconnect client for account %s: %s", account_id, e)

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

        # For destination category, filter by can_post
        if target in ("dest", "dst"):
            results = [c for c in results if c.can_post]

        cls.update_target_state(
            user_id,
            target,
            view="cat",
            category=category,
            page=page,
            chats=results,
        )

        cat_names = {
            "channel": "📢 Channels",
            "group": "👥 Groups",
            "private": "💬 Private Chats",
            "all": "📋 All Chats",
        }
        title = cat_names.get(category, "Chats")
        code = cls._normalize_target(target)
        prefix = f"cp:{code}"
        label = "Source" if code == "src" else "Destination"

        total_chats = len(results)
        total_pages = max(1, (total_chats + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start_idx = page * PAGE_SIZE
        end_idx = min(start_idx + PAGE_SIZE, total_chats)

        if not results:
            text = (
                f"{title} ({label})\n\n"
                f"No accessible {category}s found in your Telegram account."
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Refresh Chats", callback_data=f"{prefix}:refresh"
                        ),
                        InlineKeyboardButton("⬅️ Back", callback_data=f"{prefix}:menu"),
                    ]
                ]
            )
        else:
            text = (
                f"{title} — *Select {label}*\n"
                f"Showing {start_idx + 1}–{end_idx} of {total_chats} (Page {page + 1}/{total_pages})\n\n"
                "Tap a chat to select it:"
            )
            kb = cls.build_list_keyboard(
                chats=results,
                target=target,
                page=page,
                page_size=PAGE_SIZE,
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

        recents = await ChatDiscovery.get_recent_dialogs(account_id, limit=30)
        if target in ("dest", "dst"):
            recents = [c for c in recents if c.can_post]

        cls.update_target_state(
            user_id,
            target,
            view="recent",
            page=page,
            chats=recents,
        )

        code = cls._normalize_target(target)
        prefix = f"cp:{code}"
        label = "Sources" if code == "src" else "Destinations"
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
                            "🔎 Search", callback_data=f"{prefix}:search"
                        ),
                        InlineKeyboardButton(
                            "📋 All Chats", callback_data=f"{prefix}:cat:all"
                        ),
                    ],
                    [InlineKeyboardButton("⬅️ Back", callback_data=f"{prefix}:menu")],
                ]
            )
        else:
            total_chats = len(recents)
            total_pages = max(1, (total_chats + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(0, min(page, total_pages - 1))
            start_idx = page * PAGE_SIZE
            end_idx = min(start_idx + PAGE_SIZE, total_chats)

            text = (
                f"📋 *Recent {label}*\n"
                f"Showing {start_idx + 1}–{end_idx} of {total_chats} (Page {page + 1}/{total_pages})\n\n"
                "Choose from your recently selected chats:"
            )
            kb = cls.build_list_keyboard(
                chats=recents,
                target=target,
                page=page,
                page_size=PAGE_SIZE,
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

        # Restore current view with preserved query, reset to page 0 (Page 1)
        t_state = cls.get_target_state(user_id, target)
        view = t_state.get("view", "menu")

        if view == "search":
            q = t_state.get("query", "")
            await cls.handle_search_query(query, user_id, target, q, page=0)
        elif view == "cat":
            cat = t_state.get("category", "all")
            await cls.show_category(query, user_id, target, cat, page=0)
        elif view == "recent":
            await cls.show_recent_chats(query, user_id, target, page=0)
        else:
            await cls.show_picker(query, user_id, target)

    @classmethod
    async def handle_pick(
        cls, query, user_id: int, target: str, chat_id: int
    ) -> None:
        """Handle selection of a chat by chat_id for source or destination."""
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")
        t_state = cls.get_target_state(user_id, target)
        active_chats = t_state.get("chats", []) or udata.get("cp_active_chats", [])

        # Find chat info in active list or cache
        picked: Optional[DiscoveredChat] = next(
            (c for c in active_chats if c.id == chat_id), None
        )
        if not picked:
            cached = ChatDiscovery._dialog_cache.get(account_id, [])
            picked = next((c for c in cached if c.id == chat_id), None)

        if not picked:
            if chat_id == 0:
                picked = DiscoveredChat(
                    id=0,
                    title="💾 Local Downloads Folder",
                    chat_type="private",
                    display_icon="💾",
                )
            else:
                # Fallback representation
                picked = DiscoveredChat(
                    id=chat_id,
                    title=str(chat_id),
                    chat_type="chat",
                )

        # Record recent selection in database (only for valid non-zero chats)
        if picked.id != 0:
            await ChatDiscovery.record_chat_selection(account_id, picked)

        if target in ("source", "src"):
            if udata.get("is_cleaning_mode"):
                session_store.update_data(
                    user_id,
                    clean_chat_id=picked.id,
                    clean_chat_title=picked.title,
                )
                from app.transfer.cleaner import show_clean_menu

                await show_clean_menu(query, user_id, picked.id, picked.title)
                return

            client = await user_client_manager.get_client_for_account(account_id)

            # Check if source chat is a forum supergroup with topics
            if picked.is_forum and client:
                try:
                    topics = await TopicManager.get_topics(client, picked.id)
                    if topics:
                        session_store.update_data(
                            user_id,
                            source_chat_id=picked.id,
                            source_chat_title=picked.title,
                            available_source_topics=topics,
                        )
                        text = (
                            f"✅ *Source Group Selected*\n\n"
                            f"{picked.display_icon} *{picked.title}*\n\n"
                            "🧵 *This group has Topics / Subgroups enabled.*\n"
                            "Select which topic to transfer from, or transfer the entire group:"
                        )
                        buttons = [
                            [
                                InlineKeyboardButton(
                                    f"🧵 {t.title}",
                                    callback_data=f"topic:src:{t.id}",
                                )
                            ]
                            for t in topics[:20]
                        ]
                        buttons.append(
                            [
                                InlineKeyboardButton(
                                    "🌐 Entire Group (All Topics)",
                                    callback_data="topic:src:all",
                                )
                            ]
                        )
                        buttons.append(
                            [
                                InlineKeyboardButton(
                                    "⬅️ Change Source", callback_data="cp:src:menu"
                                ),
                                InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
                            ]
                        )
                        await query.edit_message_text(
                            text=text,
                            reply_markup=InlineKeyboardMarkup(buttons),
                            parse_mode="Markdown",
                        )
                        return
                except Exception as e:
                    logger.error("Failed to retrieve source forum topics: %s", e)

            if udata.get("is_local_download"):
                session_store.update_data(
                    user_id,
                    source_chat_id=picked.id,
                    source_chat_title=picked.title,
                    source_thread_id=None,
                    source_topic_name=None,
                    destination_chat_id=0,
                    destination_chat_title="💾 Downloads Folder",
                    destination_thread_id=None,
                    topic_name=None,
                )
                from app.bot.callbacks import _show_content_filter

                await _show_content_filter(query, user_id)
                return

            session_store.update_data(
                user_id,
                source_chat_id=picked.id,
                source_chat_title=picked.title,
                source_thread_id=None,
                source_topic_name=None,
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


        elif target in ("dest", "dst"):
            if picked.id == 0:
                session_store.update_data(
                    user_id,
                    destination_chat_id=0,
                    destination_chat_title="💾 Downloads Folder",
                    destination_thread_id=None,
                    topic_name=None,
                )
                from app.bot.callbacks import _show_content_filter

                await _show_content_filter(query, user_id)
                return

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
        if reason and any(k in reason.lower() for k in ("storage", "readonly", "read-only", "disk i/o", "operationalerror")):
            text = (
                "❌ *Telegram session storage error.*\n\n"
                "The Telegram account session could not be written.\n\n"
                "Please reconnect the Telegram account from:\n"
                "*Connected Accounts* → *Reconnect*"
            )
        else:
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
