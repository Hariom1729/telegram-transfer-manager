"""Callback Query Handlers for Navigation and Wizards."""

import logging
from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telethon import TelegramClient
from app.bot.chat_picker import ChatPicker
from app.bot.keyboards import (
    build_accounts_keyboard,
    build_auth_code_keyboard,
    build_cancel_confirmation_keyboard,
    build_chat_list_keyboard,
    build_chat_selection_modes_keyboard,
    build_content_filters_keyboard,
    build_duplicate_mode_keyboard,
    build_job_completion_keyboard,
    build_main_menu_keyboard,
    build_paused_keyboard,
    build_preview_keyboard,
    build_range_keyboard,
    build_topics_keyboard,
)
from app.bot.middleware import check_authorized
from app.bot.states import BotState, session_store
from app.models.transfer_job import JobStatus
from app.telegram.discovery import ChatDiscovery
from app.telegram.topics import TopicManager
from app.telegram.user_client import user_client_manager
from app.transfer.manager import transfer_manager
from app.transfer.worker import transfer_worker
from app.utils.formatting import format_progress_bar

logger = logging.getLogger(__name__)


@check_authorized
async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route callback queries based on prefix."""
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    user_id = update.effective_user.id
    await query.answer()

    # 1. Navigation Actions
    if data == "nav:home":
        session_store.clear(user_id)
        welcome_text = (
            "🤖 *Telegram Transfer Manager*\n\n"
            "Welcome!\n\n"
            "Transfer content between Telegram chats\n"
            "using a simple Telegram interface.\n\n"
            "Choose an action:"
        )
        await query.edit_message_text(
            text=welcome_text,
            reply_markup=build_main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    elif data == "nav:help":
        help_text = (
            "📖 *Telegram Transfer Manager Help*\n\n"
            "• *New Transfer*: Step-by-step wizard to copy channels, groups, or topics.\n"
            "• *Connected Accounts*: Authenticate Telegram user sessions (MTProto) for private chats & channels.\n"
            "• *Single File Transfer*: Send any document/video/photo directly to this bot to forward/copy to any chat/topic.\n"
            "• *Duplicate Prevention*: Uses database message mapping to skip already transferred content.\n"
            "• *FloodWait Recovery*: Automatically pauses and waits without losing transfer position.\n"
            "• *Pause / Resume*: Safely halt transfers and resume without duplicates.\n\n"
            "Use /cancel anytime to return to the main menu."
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]
        )
        await query.edit_message_text(
            text=help_text, reply_markup=kb, parse_mode="Markdown"
        )
        return

    elif data == "nav:settings":
        from app.bot.commands import cmd_settings
        await cmd_settings(update, context)
        return

    elif data == "nav:webapp":
        webapp_text = (
            "✨ *Modern Web Dashboard & Telegram Mini App*\n\n"
            "The Transfer Manager includes a full modern glassmorphic web dashboard!\n\n"
            "🌐 *How to Access:*\n"
            "• **Local Browser:** `http://localhost:7860`\n"
            "• **Hugging Face / VPS:** Accessible directly on your app's public URL port `7860`\n\n"
            "📱 *Enable Telegram Mini App in Telegram:*\n"
            "Add `WEBAPP_URL=https://your-public-url` in your `.env` file. "
            "This button will then open the dashboard directly inside Telegram!"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="nav:home")]]
        )
        await query.edit_message_text(text=webapp_text, reply_markup=kb, parse_mode="Markdown")
        return

    elif data == "nav:cancel":
        was_in_auth = session_store.get_state(user_id) in (
            BotState.AUTH_WAITING_PHONE,
            BotState.AUTH_WAITING_CODE,
            BotState.AUTH_WAITING_2FA,
        )
        await user_client_manager.cancel_login(user_id)
        session_store.clear(user_id)

        if was_in_auth:
            accounts = await user_client_manager.list_user_accounts(user_id)
            text = (
                "🔗 *Connected Accounts*\n\n"
                f"Active MTProto Sessions: {len(accounts)}\n\n"
                "Connect an account to access channels and groups you are a member of."
            )
            await query.edit_message_text(
                text=text,
                reply_markup=build_accounts_keyboard(accounts),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                text="❌ Operation cancelled.",
                reply_markup=build_main_menu_keyboard(),
            )
        return

    elif data == "acc:new_code":
        success, msg = await user_client_manager.request_new_code(user_id)
        if success:
            session_store.set_state(user_id, BotState.AUTH_WAITING_CODE)
            await query.edit_message_text(
                text=(
                    "📩 *New Login Code Sent!*\n\n"
                    "Telegram has sent a new login code to your Telegram app. "
                    "Please enter the code below:\n\n"
                    "(Tip: Enter digits only, e.g. `12345`)"
                ),
                reply_markup=build_auth_code_keyboard(),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                text=f"❌ {msg}\n\nPlease try again or click Cancel:",
                reply_markup=build_auth_code_keyboard(),
                parse_mode="Markdown",
            )
        return

    elif data == "nav:accounts":
        accounts = await user_client_manager.list_user_accounts(user_id)
        text = (
            "🔗 *Connected Accounts*\n\n"
            f"Active MTProto Sessions: {len(accounts)}\n\n"
            "Connect an account to access channels and groups you are a member of."
        )
        await query.edit_message_text(
            text=text,
            reply_markup=build_accounts_keyboard(accounts),
            parse_mode="Markdown",
        )
        return

    elif data == "acc:connect":
        session_store.set_state(user_id, BotState.AUTH_WAITING_PHONE)
        text = (
            "📱 *Connect Telegram Account*\n\n"
            "Please enter your Telegram phone number with country code:\n\n"
            "Example:\n"
            "`+919876543210`\n\n"
            "Send /cancel to abort."
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")]]
        )
        await query.edit_message_text(
            text=text, reply_markup=kb, parse_mode="Markdown"
        )
        return

    elif data.startswith("acc:disconnect:"):
        acc_id = int(data.split(":")[-1])
        await user_client_manager.disconnect_account(acc_id)
        accounts = await user_client_manager.list_user_accounts(user_id)
        await query.edit_message_text(
            text="🗑 Account disconnected.",
            reply_markup=build_accounts_keyboard(accounts),
        )
        return

    elif data.startswith("acc:reconnect:"):
        acc_id = int(data.split(":")[-1])
        await query.answer("🔄 Reconnecting Telegram account...")
        success, msg = await user_client_manager.reconnect_account(acc_id)
        accounts = await user_client_manager.list_user_accounts(user_id)
        if success:
            text = (
                "✅ *Account Reconnected Successfully!*\n\n"
                "Your Telegram session is active and ready for transfers and discovery."
            )
        else:
            text = (
                f"❌ *Could not reconnect account.*\n\n"
                f"Reason: _{msg}_\n\n"
                "You can try logging in again with ➕ Connect Telegram Account."
            )
        await query.edit_message_text(
            text=text,
            reply_markup=build_accounts_keyboard(accounts),
            parse_mode="Markdown",
        )
        return

    elif data == "nav:active":
        active_jobs = await transfer_manager.get_active_jobs()
        if not active_jobs:
            text = "📊 *Active Transfers*\n\nNo transfers are currently running or queued."
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]
            )
        else:
            text = f"📊 *Active Transfers* ({len(active_jobs)})\n\n"
            buttons = []
            for j in active_jobs:
                p_bar = format_progress_bar(
                    j.processed_messages, j.total_messages, length=10
                )
                status_icon = "🟢" if j.status == JobStatus.RUNNING.value else "⏸"
                text += (
                    f"{status_icon} *#{j.id}*\n"
                    f"{j.source_chat_title or j.source_chat_id} → {j.destination_chat_title or j.destination_chat_id}\n"
                    f"{p_bar} ({j.processed_messages}/{j.total_messages})\n\n"
                )
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"Control #{j.id}",
                            callback_data=f"job_view:{j.id}",
                        )
                    ]
                )
            buttons.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
            kb = InlineKeyboardMarkup(buttons)

        await query.edit_message_text(
            text=text, reply_markup=kb, parse_mode="Markdown"
        )
        return

    elif data == "nav:history":
        history = await transfer_manager.get_history(user_id, limit=8)
        if not history:
            text = "📋 *Transfer History*\n\nNo past transfers found."
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]
            )
        else:
            text = "📋 *Transfer History*\n\n"
            buttons = []
            for j in history:
                icon = (
                    "✅"
                    if j.status == JobStatus.COMPLETED.value
                    else ("❌" if j.status == JobStatus.FAILED.value else "⏸")
                )
                text += (
                    f"*{icon} #{j.id}*\n"
                    f"{j.source_chat_title or j.source_chat_id} → {j.destination_chat_title or j.destination_chat_id}\n"
                    f"Status: {j.status} | Messages: {j.successful_messages}/{j.processed_messages}\n\n"
                )
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"View #{j.id}", callback_data=f"job_view:{j.id}"
                        )
                    ]
                )
            buttons.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
            kb = InlineKeyboardMarkup(buttons)

        await query.edit_message_text(
            text=text, reply_markup=kb, parse_mode="Markdown"
        )
        return

    # 2. Transfer Creation Flow
    elif data == "nav:new_transfer":
        accounts = await user_client_manager.list_user_accounts(user_id)
        if not accounts:
            text = (
                "⚠️ *No Connected Telegram Accounts*\n\n"
                "To transfer content from your chats, you must first connect your Telegram account."
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "➕ Connect Account", callback_data="acc:connect"
                        )
                    ],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]
            )
            await query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
            return

        # Select account
        session_store.clear(user_id)
        if len(accounts) == 1:
            session_store.update_data(user_id, account_id=accounts[0].id)
            await ChatPicker.show_source_picker(query, user_id)
        else:
            text = "👤 *Select Telegram Account* to use for this transfer:"
            buttons = [
                [
                    InlineKeyboardButton(
                        f"👤 {acc.first_name or acc.username or acc.phone_number}",
                        callback_data=f"wizard:acc:{acc.id}",
                    )
                ]
                for acc in accounts
            ]
            buttons.append(
                [InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")]
            )
            await query.edit_message_text(
                text=text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
        return

    elif data == "nav:new_download":
        accounts = await user_client_manager.list_user_accounts(user_id)
        if not accounts:
            text = (
                "⚠️ *No Connected Telegram Accounts*\n\n"
                "To download content to local storage, you must first connect your Telegram account."
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "➕ Connect Account", callback_data="acc:connect"
                        )
                    ],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]
            )
            await query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
            return

        session_store.clear(user_id)
        session_store.update_data(
            user_id,
            is_local_download=True,
            destination_chat_id=0,
            destination_chat_title="💾 Downloads Folder",
        )
        if len(accounts) == 1:
            session_store.update_data(user_id, account_id=accounts[0].id)
            await ChatPicker.show_source_picker(query, user_id)
        else:
            text = "👤 *Select Telegram Account* to use for this download:"
            buttons = [
                [
                    InlineKeyboardButton(
                        f"👤 {acc.first_name or acc.username or acc.phone_number}",
                        callback_data=f"wizard:acc:{acc.id}",
                    )
                ]
                for acc in accounts
            ]
            buttons.append(
                [InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")]
            )
            await query.edit_message_text(
                text=text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
        return

    elif data == "nav:clean":
        accounts = await user_client_manager.list_user_accounts(user_id)
        if not accounts:
            text = (
                "⚠️ *No Connected Telegram Accounts*\n\n"
                "To clean channels or remove duplicates, you must first connect your Telegram account."
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "➕ Connect Account", callback_data="acc:connect"
                        )
                    ],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                ]
            )
            await query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
            return

        session_store.clear(user_id)
        session_store.update_data(user_id, is_cleaning_mode=True)
        if len(accounts) == 1:
            session_store.update_data(user_id, account_id=accounts[0].id)
            await ChatPicker.show_source_picker(query, user_id)
        else:
            text = "👤 *Select Telegram Account* to use for channel cleaning:"
            buttons = [
                [
                    InlineKeyboardButton(
                        f"👤 {acc.first_name or acc.username or acc.phone_number}",
                        callback_data=f"wizard:acc:{acc.id}",
                    )
                ]
                for acc in accounts
            ]
            buttons.append(
                [InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")]
            )
            await query.edit_message_text(
                text=text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
        return

    elif data.startswith("clean:"):
        from app.transfer.cleaner import handle_clean_callback

        await handle_clean_callback(update, user_id, data)
        return

    elif data.startswith("wizard:acc:"):

        acc_id = int(data.split(":")[-1])
        session_store.update_data(user_id, account_id=acc_id)
        await ChatPicker.show_source_picker(query, user_id)
        return

    # Chat Picker Callbacks (Source & Destination)
    elif data.startswith("cp:"):
        await _handle_chat_picker_callback(query, user_id, data)
        return

    # Fallback legacy Source / Destination Selection
    elif data.startswith("src:"):
        await ChatPicker.show_source_picker(query, user_id)
        return

    elif data.startswith("dst:"):
        await ChatPicker.show_destination_picker(query, user_id)
        return

    # Topic Selection
    elif data.startswith("topic:"):
        await _handle_topic_action(query, user_id, data)
        return

    # Content Filter
    elif data.startswith("content:"):
        await _handle_content_action(query, user_id, data)
        return

    # Browse Messages
    elif data.startswith("browse:"):
        await _handle_browse_action(query, user_id, data)
        return

    # Range Selection
    elif data.startswith("range:"):
        await _handle_range_action(query, user_id, data)
        return

    # Duplicate Handling
    elif data.startswith("dup:"):
        await _handle_dup_action(query, user_id, data)
        return

    # Preview & Start
    elif data.startswith("preview:"):
        await _handle_preview_action(query, user_id, data)
        return

    # Job Control Callbacks
    elif data.startswith("job_"):
        await _handle_job_control_action(query, user_id, data)
        return


async def _handle_chat_picker_callback(query, user_id: int, data: str) -> None:
    """Handle all ChatPicker interactions."""
    if data == "cp:src:to_dest":
        await ChatPicker.show_destination_picker(query, user_id)
        return

    parts = data.split(":")
    if len(parts) >= 3 and parts[1] == "retry":
        target = parts[2]
        if target == "source":
            await ChatPicker.show_source_picker(query, user_id)
        else:
            await ChatPicker.show_destination_picker(query, user_id)
        return

    if len(parts) >= 2:
        tgt_code = parts[1]
        target = "source" if tgt_code == "src" else "dest"
        action = parts[2] if len(parts) > 2 else "menu"

        if action == "menu":
            await ChatPicker.show_picker(query, user_id, target)
        elif action == "search":
            await ChatPicker.show_search_prompt(query, user_id, target)
        elif action == "cat":
            cat = parts[3] if len(parts) > 3 else "all"
            await ChatPicker.show_category(query, user_id, target, cat, page=0)
        elif action == "recent":
            await ChatPicker.show_recent_chats(query, user_id, target, page=0)
        elif action == "refresh":
            await ChatPicker.refresh_dialogs(query, user_id, target)
        elif action == "pg":
            page = int(parts[3]) if len(parts) > 3 else 0
            t_state = ChatPicker.get_target_state(user_id, target)
            view = t_state.get("view", "cat")
            if view == "search":
                q = t_state.get("query", "")
                await ChatPicker.handle_search_query(query, user_id, target, q, page=page)
            elif view == "recent":
                await ChatPicker.show_recent_chats(query, user_id, target, page=page)
            else:
                cat = t_state.get("category", "all")
                await ChatPicker.show_category(query, user_id, target, cat, page=page)
        elif action == "pk":
            chat_id = int(parts[3]) if len(parts) > 3 else 0
            await ChatPicker.handle_pick(query, user_id, target, chat_id)


async def _handle_topic_action(query, user_id: int, data: str) -> None:
    """Handle forum topic selection, creation, and refresh."""
    udata = session_store.get_data(user_id)
    account_id = udata.get("account_id")
    dest_id = udata.get("destination_chat_id")
    dest_title = udata.get("destination_chat_title", "Destination")
    client = await user_client_manager.get_client_for_account(account_id)

    if data.startswith("topic:src:"):
        source_title = udata.get("source_chat_title", "Source")
        if data == "topic:src:all":
            session_store.update_data(
                user_id,
                source_thread_id=None,
                source_topic_name="All Topics",
            )
            topic_display = "🌐 Entire Group (All Topics)"
        else:
            topic_id = int(data.split(":")[-1])
            topics = udata.get("available_source_topics", [])
            topic_name = next(
                (t.title for t in topics if t.id == topic_id), f"Topic #{topic_id}"
            )
            session_store.update_data(
                user_id,
                source_thread_id=topic_id,
                source_topic_name=topic_name,
            )
            topic_display = f"🧵 {topic_name}"

        if udata.get("is_local_download"):
            session_store.update_data(
                user_id,
                destination_chat_id=0,
                destination_chat_title="💾 Downloads Folder",
                destination_thread_id=None,
                topic_name=None,
            )
            await _show_content_filter(query, user_id)
            return

        text = (
            f"✅ *Source Selected*\n\n"
            f"📢 *{source_title}*\n"
            f"{topic_display}\n\n"
            "Choose what to transfer:"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📂 Browse Messages",
                        callback_data="browse:open",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔢 Enter Message ID/Range",
                        callback_data="browse:enter_range",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "➡️ All Messages (Select Destination)",
                        callback_data="cp:src:to_dest",
                    )
                ],
                [
                    InlineKeyboardButton("⬅️ Back", callback_data="cp:src:menu"),
                    InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel"),
                ],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    if data.startswith("topic:pick:"):

        topic_id = int(data.split(":")[-1])
        topics = udata.get("available_topics", [])
        topic_name = next(
            (t.title for t in topics if t.id == topic_id), f"Topic #{topic_id}"
        )
        session_store.update_data(
            user_id,
            destination_thread_id=topic_id,
            topic_name=topic_name,
        )
        await _show_content_filter(query, user_id)
        return

    if data == "topic:back":
        await ChatPicker.show_destination_picker(query, user_id)
        return

    if data == "topic:refresh":
        if not client or not dest_id:
            await query.answer("❌ Account or destination chat not found.", show_alert=True)
            return

        await query.answer("🔄 Refreshing topics from Telegram...")
        topics = await TopicManager.get_topics(client, dest_id, force_refresh=True)
        session_store.update_data(user_id, available_topics=topics)

        text = (
            f"✅ *Destination Selected*\n\n"
            f"👥 *{dest_title}*\n\n"
            "🧵 *This group has Topics enabled.*\n"
            "Select a destination topic:"
        )
        await query.edit_message_text(
            text=text,
            reply_markup=build_topics_keyboard(topics),
            parse_mode="Markdown",
        )
        return

    if data == "topic:create":
        if not client or not dest_id:
            await query.edit_message_text("❌ Account session unavailable.")
            return

        try:
            entity = await client.get_entity(dest_id)
            can_create, reason = TopicManager.check_topic_permissions(entity)
            if not can_create:
                if reason in ("NOT_A_SUPERGROUP", "NOT_A_FORUM"):
                    err_text = "❌ *Topics aren't available in this destination.*"
                else:
                    err_text = "❌ *Your connected Telegram account cannot create topics here.*"
                kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back", callback_data="cp:dst:menu")]]
                )
                await query.edit_message_text(
                    text=err_text, reply_markup=kb, parse_mode="Markdown"
                )
                return
        except Exception as e:
            logger.warning("Failed to check topic permissions: %s", e)

        session_store.set_state(user_id, BotState.WIZARD_TOPIC_CREATE)
        text = (
            "🆕 *Create Topic*\n\n"
            "Enter the topic name (e.g. `Python Course`):"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="cp:dst:menu")]]
        )
        await query.edit_message_text(
            text=text, reply_markup=kb, parse_mode="Markdown"
        )
        return


async def _show_content_filter(query, user_id: int) -> None:
    """Show content type filter selection."""
    data = session_store.get_data(user_id)
    selected_types = data.get("content_types", ["all"])
    session_store.update_data(user_id, content_types=selected_types)

    is_local = (data.get("destination_chat_id", 0) == 0) or bool(data.get("is_local_download"))
    if is_local:
        text = (
            "💾 *Local Download — Content Filter*\n\n"
            "Select what file types to save to your local folder:\n"
            "(Default: Everything)"
        )
    else:
        text = (
            "⚙️ *Transfer Settings — Content*\n\n"
            "Select what types of content to transfer:\n"
            "(Default: Everything)"
        )

    await query.edit_message_text(
        text=text,
        reply_markup=build_content_filters_keyboard(selected_types),
        parse_mode="Markdown",
    )


async def _handle_content_action(query, user_id: int, data: str) -> None:
    """Handle content toggles."""
    user_data = session_store.get_data(user_id)
    current = list(user_data.get("content_types", ["all"]))

    if data.startswith("content:toggle:"):
        item = data.split(":")[-1]
        if item == "all":
            current = ["all"]
        else:
            if "all" in current:
                current.remove("all")
            if item in current:
                current.remove(item)
            else:
                current.append(item)
            if not current:
                current = ["all"]

        session_store.update_data(user_id, content_types=current)
        await query.edit_message_reply_markup(
            reply_markup=build_content_filters_keyboard(current)
        )
        return

    if data == "content:done":
        # Show range selection
        is_local = (user_data.get("destination_chat_id", 0) == 0) or bool(user_data.get("is_local_download"))
        if is_local:
            text = (
                "💾 *Local Download — Quantity & Range*\n\n"
                "Choose how many files/messages to download to local disk:"
            )
        else:
            text = (
                "📅 *Transfer Settings — Message Range*\n\n"
                "Choose which messages to transfer:"
            )
        await query.edit_message_text(
            text=text,
            reply_markup=build_range_keyboard(),
            parse_mode="Markdown",
        )
        return


async def _handle_browse_action(query, user_id: int, data: str) -> None:
    """Handle all message browser actions."""
    from app.bot.message_browser import MessageBrowser

    if data == "browse:open":
        await MessageBrowser.show_browser(query, user_id, reset=True)
        return

    if data == "browse:refresh":
        await MessageBrowser.show_browser(query, user_id, reset=False)
        return

    if data == "browse:next":
        await MessageBrowser.handle_next(query, user_id)
        return

    if data == "browse:prev":
        await MessageBrowser.handle_prev(query, user_id)
        return

    if data == "browse:toggle_video":
        await MessageBrowser.toggle_video(query, user_id)
        return

    if data == "browse:search":
        session_store.set_state(user_id, BotState.WIZARD_BROWSE_SEARCH)
        text = (
            "🔍 *Search Messages*\n\n"
            "Send the keyword or text you want to search for in this chat:"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Browser", callback_data="browse:refresh")]]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    if data == "browse:clear_search":
        await MessageBrowser.clear_search(query, user_id)
        return

    if data == "browse:jump":
        session_store.set_state(user_id, BotState.WIZARD_BROWSE_JUMP)
        text = (
            "🔢 *Jump to Message ID*\n\n"
            "Send the Telegram message ID you want to jump to (e.g. `1245`):"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Browser", callback_data="browse:refresh")]]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    if data in ("browse:select_range", "browse:enter_range"):
        session_store.set_state(user_id, BotState.WIZARD_RANGE_INPUT)
        back_cb = "browse:refresh" if data == "browse:select_range" else "browse:back"
        text = (
            "🔢 *Enter Message ID or Range*\n\n"
            "Send any of the following formats:\n"
            "• **Single message:** `1245`\n"
            "• **Message range:** `1240-1250` or `1240 1250`\n"
            "• **Recent messages:** `first 10` or `last 20`"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data=back_cb)]]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return

    if data == "browse:to_dest":
        udata = session_store.get_data(user_id)
        if udata.get("destination_chat_id") is not None:
            await _show_content_filter(query, user_id)
        else:
            await ChatPicker.show_destination_picker(query, user_id)
        return

    if data == "browse:back":
        udata = session_store.get_data(user_id)
        source_title = udata.get("source_chat_title", "Source Chat")
        source_topic = udata.get("source_topic_name")
        topic_display = f"\n🧵 `{source_topic}`" if source_topic else ""
        text = (
            f"✅ *Source Selected*\n\n"
            f"📢 *{source_title}*{topic_display}\n\n"
            "Choose what to transfer:"
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📂 Browse Messages", callback_data="browse:open")],
                [InlineKeyboardButton("🔢 Enter Message ID/Range", callback_data="browse:enter_range")],
                [InlineKeyboardButton("➡️ All Messages (Select Destination)", callback_data="cp:src:to_dest")],
                [InlineKeyboardButton("⬅️ Back", callback_data="cp:src:menu"), InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")],
            ]
        )
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode="Markdown")
        return


async def _handle_range_action(query, user_id: int, data: str) -> None:
    """Handle range selection."""
    udata = session_store.get_data(user_id)
    is_local = (udata.get("destination_chat_id", 0) == 0) or bool(udata.get("is_local_download"))

    if data == "range:back":
        await _show_content_filter(query, user_id)
        return

    if data.startswith("range:pick:"):
        r_type = data.split(":")[-1]
        if r_type == "custom":
            session_store.set_state(user_id, BotState.WIZARD_RANGE_INPUT)
            text = (
                "🔢 *Enter Message Range or Quantity*\n\n"
                "Send any of the following formats:\n"
                "• **Single message:** `1245`\n"
                "• **Specific ID range:** `1240-1250` or `1240 1250`\n"
                "• **Recent messages:** `first 10` or `last 20`"
            )
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="content:done")]]
            )
            await query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
            return

        # Numeric limits: first_5, first_10, 50, 100, 500, 1000 or all
        if r_type == "all":
            limit = None
        elif "_" in r_type:
            limit = int(r_type.split("_")[-1])
        else:
            limit = int(r_type)

        session_store.update_data(
            user_id,
            range_type=r_type,
            range_limit=limit,
            start_message_id=None,
            end_message_id=None,
        )

        # Move to Duplicate handling
        if is_local:
            text = (
                "💾 *Local Download — Duplicate Handling*\n\n"
                "Choose how to handle files that already exist on disk:"
            )
        else:
            text = (
                "🔁 *Transfer Settings — Duplicate Handling*\n\n"
                "Choose how to handle messages that were already transferred:"
            )
        await query.edit_message_text(
            text=text,
            reply_markup=build_duplicate_mode_keyboard("skip"),
            parse_mode="Markdown",
        )
        return


async def _handle_dup_action(query, user_id: int, data: str) -> None:
    """Handle duplicate selection."""
    if data.startswith("dup:set:"):
        mode = data.split(":")[-1]
        session_store.update_data(user_id, duplicate_mode=mode)
        await query.edit_message_reply_markup(
            reply_markup=build_duplicate_mode_keyboard(mode)
        )
        return

    if data == "dup:done":
        # Show Transfer/Download Preview!
        await _show_transfer_preview(query, user_id)
        return


async def _show_transfer_preview(query, user_id: int) -> None:
    """Format and show Section 14 Preview."""
    udata = session_store.get_data(user_id)
    source_title = udata.get("source_chat_title", "Unknown")
    dest_id = udata.get("destination_chat_id", 0)
    dest_title = udata.get("destination_chat_title", "Unknown")
    is_local = (dest_id == 0) or ("Local Storage" in str(dest_title))
    dest_icon = "💾" if is_local else "👥"
    topic_name = udata.get("topic_name")
    topic_line = f"\nTOPIC\n🧵 {topic_name}\n" if (topic_name and not is_local) else ""

    src_topic_name = udata.get("source_topic_name")
    src_topic_line = f"🧵 Topic: {src_topic_name}\n" if (src_topic_name and src_topic_name != "All Topics") else ""

    c_types = udata.get("content_types", ["all"])
    content_str = "📦 Everything" if "all" in c_types else ", ".join(c_types)

    r_limit = udata.get("range_limit")
    s_id = udata.get("start_message_id")
    e_id = udata.get("end_message_id")
    if s_id and e_id:
        if s_id == e_id:
            range_str = f"Single Message #{s_id}"
        else:
            range_str = f"Messages {s_id} → {e_id}"
    elif r_limit:
        range_str = f"Last {r_limit} messages"
    else:
        range_str = "All Messages"

    dup_mode = udata.get("duplicate_mode", "skip")
    dup_str = "⏭ Skip existing" if dup_mode == "skip" else "🔄 Transfer again"

    header = "💾 *Local Download Preview*" if is_local else "🚀 *Transfer Preview*"

    # Calculate accessible messages found & video messages found (Requirement 6)
    video_only = ("video" in c_types) or bool(udata.get("browse_video_only", False))
    accessible_count = None
    video_count = None

    account_id = udata.get("account_id")
    source_id = udata.get("source_chat_id")
    source_thread_id = udata.get("source_thread_id")

    if account_id and source_id:
        try:
            from app.transfer.copier import MessageCopier
            client = await user_client_manager.get_active_client(account_id)
            if client and client.is_connected():
                source_entity = await client.get_entity(source_id)
                if s_id and e_id:
                    if s_id == e_id:
                        msg = await client.get_messages(source_entity, ids=s_id)
                        if msg and getattr(msg, "id", None) and not getattr(msg, "empty", False):
                            accessible_count = 1
                            is_v = MessageCopier.classify_message_content(msg) == "video"
                            video_count = 1 if is_v else 0
                        else:
                            accessible_count = 0
                            video_count = 0
                    else:
                        acc = 0
                        vid = 0
                        async for msg in client.iter_messages(
                            source_entity,
                            min_id=max(0, s_id - 1),
                            max_id=e_id + 1,
                            reply_to=source_thread_id if source_thread_id else None,
                        ):
                            if msg and getattr(msg, "id", None) and not getattr(msg, "empty", False):
                                if s_id <= msg.id <= e_id:
                                    acc += 1
                                    if MessageCopier.classify_message_content(msg) == "video":
                                        vid += 1
                        accessible_count = acc
                        video_count = vid
                elif r_limit:
                    acc = 0
                    vid = 0
                    async for msg in client.iter_messages(
                        source_entity,
                        limit=r_limit,
                        reply_to=source_thread_id if source_thread_id else None,
                    ):
                        if msg and getattr(msg, "id", None) and not getattr(msg, "empty", False):
                            acc += 1
                            if MessageCopier.classify_message_content(msg) == "video":
                                vid += 1
                    accessible_count = acc
                    video_count = vid
                else:
                    count_res = await client.get_messages(
                        source_entity, limit=0, reply_to=source_thread_id if source_thread_id else None
                    )
                    accessible_count = getattr(count_res, "total", None)
                    if video_only:
                        from telethon.tl.types import InputMessagesFilterVideo
                        try:
                            v_res = await client.get_messages(
                                source_entity, limit=0, filter=InputMessagesFilterVideo(), reply_to=source_thread_id if source_thread_id else None
                            )
                            video_count = getattr(v_res, "total", None)
                        except Exception:
                            video_count = None
        except Exception as e:
            logger.debug("Failed computing preview message counts: %s", e)

    stats_lines = []
    if accessible_count is not None:
        stats_lines.append(f"• Accessible Messages Found: *{accessible_count}*")
    if video_only:
        if video_count is not None:
            stats_lines.append(f"• Video Messages Found: *{video_count}*")
        else:
            stats_lines.append("• Video Only Filter: *Active*")
    elif video_count is not None:
        stats_lines.append(f"• Video Messages Found: *{video_count}*")

    stats_block = ("\n\nSCAN PREVIEW\n" + "\n".join(stats_lines)) if stats_lines else ""

    session_store.update_data(
        user_id,
        accessible_count=accessible_count,
        video_count=video_count,
    )

    preview_text = (
        f"{header}\n\n"
        f"SOURCE\n📢 {source_title}\n{src_topic_line}\n"
        f"DESTINATION\n{dest_icon} {dest_title}\n"
        f"{topic_line}\n"
        f"CONTENT\n{content_str}\n\n"
        f"RANGE\n{range_str}"
        f"{stats_block}\n\n"
        f"DUPLICATES\n{dup_str}\n\n"
        "Ready to start?"
    )
    await query.edit_message_text(
        text=preview_text,
        reply_markup=build_preview_keyboard(),
        parse_mode="Markdown",
    )


async def _handle_preview_action(query, user_id: int, data: str) -> None:
    """Start the transfer job from preview."""
    if data == "preview:edit":
        await _show_content_filter(query, user_id)
        return

    if data == "preview:start":
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")
        source_id = udata.get("source_chat_id")
        source_title = udata.get("source_chat_title")
        source_thread_id = udata.get("source_thread_id")
        source_topic_name = udata.get("source_topic_name")
        dest_id = udata.get("destination_chat_id", 0)
        dest_title = udata.get("destination_chat_title")
        dest_thread_id = udata.get("destination_thread_id")
        topic_name = udata.get("topic_name")
        c_types = ",".join(udata.get("content_types", ["all"]))
        dup_mode = udata.get("duplicate_mode", "skip")
        start_id = udata.get("start_message_id")
        end_id = udata.get("end_message_id")
        r_limit = udata.get("range_limit")
        accessible_count = udata.get("accessible_count")

        total_msgs = accessible_count if (accessible_count is not None and accessible_count > 0) else (r_limit if r_limit else 0)

        # Create job
        job = await transfer_manager.create_job(
            owner_id=user_id,
            telegram_account_id=account_id,
            source_chat_id=source_id,
            source_chat_title=source_title,
            source_thread_id=source_thread_id,
            source_topic_name=source_topic_name,
            destination_chat_id=dest_id,
            destination_chat_title=dest_title,
            destination_thread_id=dest_thread_id,
            topic_name=topic_name,
            content_types=c_types,
            duplicate_mode=dup_mode,
            start_message_id=start_id,
            end_message_id=end_id,
            total_messages=total_msgs,
        )


        chat_id = query.message.chat_id
        message_id = query.message.message_id

        # Register live edit callback
        async def edit_progress(text: str, reply_markup):
            try:
                await query.get_bot().edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            except Exception:
                pass

        transfer_worker.register_progress_callback(job.id, edit_progress)

        # Enqueue job
        await transfer_manager.start_job(job.id)

        session_store.clear(user_id)
        await query.edit_message_text(
            f"🚀 Transfer #{job.id} queued!\nStarting transfer engine..."
        )
        return


async def _handle_job_control_action(query, user_id: int, data: str) -> None:
    """Pause, resume, or cancel jobs."""
    parts = data.split(":")
    action = parts[0]
    job_id = int(parts[1]) if len(parts) > 1 else 0

    if action == "job_pause":
        await transfer_manager.pause_job(job_id)
        await query.edit_message_reply_markup(
            reply_markup=build_paused_keyboard(job_id)
        )
        await query.answer("Transfer pausing...")
        return

    if action == "job_resume":
        await transfer_manager.resume_job(job_id)
        await query.answer("Transfer resumed!")
        return

    if action == "job_cancel_prompt":
        await query.edit_message_reply_markup(
            reply_markup=build_cancel_confirmation_keyboard(job_id)
        )
        return

    if action == "job_cancel_confirm":
        await transfer_manager.cancel_job(job_id)
        await query.edit_message_text(
            f"❌ Transfer #{job_id} cancelled.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    if action == "job_keep_running":
        # Restore normal progress keyboard
        tracker = transfer_worker.get_tracker(job_id)
        if tracker:
            await query.edit_message_reply_markup(
                reply_markup=tracker.get_control_keyboard()
            )
        return

    if action in ("job_retry", "job_retry_failed"):
        job = await transfer_manager.get_job(job_id)
        if not job:
            await query.answer("Job not found.", show_alert=True)
            return

        retry_job = await transfer_manager.retry_failed_messages(job_id)
        if retry_job:
            chat_id = query.message.chat_id
            message_id = query.message.message_id

            async def edit_progress(text: str, reply_markup):
                try:
                    await query.get_bot().edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=reply_markup,
                    )
                except Exception:
                    pass

            transfer_worker.register_progress_callback(retry_job.id, edit_progress)
            await transfer_manager.start_job(retry_job.id)
            await query.answer(f"Retransferring {retry_job.total_messages} failed message(s)...")
            await query.edit_message_text(
                f"🔄 Retransferring {retry_job.total_messages} failed message(s) (Transfer #{retry_job.id})...\nStarting transfer engine..."
            )
            return
        else:
            await transfer_manager.retry_failed_job(job_id)
            await query.answer("Retrying transfer...")
            return

    if action == "job_view":
        job = await transfer_manager.get_job(job_id)
        if not job:
            await query.edit_message_text("Job not found.")
            return

        detail_text = (
            f"📦 *Transfer Details #{job.id}*\n\n"
            f"Status: *{job.status}*\n"
            f"Source: {job.source_chat_title or job.source_chat_id}\n"
            f"Destination: {job.destination_chat_title or job.destination_chat_id}\n"
            f"Topic: {job.topic_name or 'None'}\n\n"
            f"Processed: {job.processed_messages}\n"
            f"✅ Success: {job.successful_messages}\n"
            f"⏭ Skipped: {job.skipped_messages}\n"
            f"❌ Failed: {job.failed_messages}\n"
        )
        if job.error_summary:
            detail_text += f"\nError: {job.error_summary}\n"

        buttons = []
        if job.failed_messages > 0:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"🔄 Retransfer {job.failed_messages} Failed Msg",
                        callback_data=f"job_retry_failed:{job.id}",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
        kb = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(
            text=detail_text, reply_markup=kb, parse_mode="Markdown"
        )
        return

