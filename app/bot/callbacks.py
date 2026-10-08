"""Callback Query Handlers for Navigation and Wizards."""

import logging
from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telethon import TelegramClient
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
            text="🗑 Account disconnected and session removed.",
            reply_markup=build_accounts_keyboard(accounts),
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
            await _show_source_modes(query, user_id)
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

    elif data.startswith("wizard:acc:"):
        acc_id = int(data.split(":")[-1])
        session_store.update_data(user_id, account_id=acc_id)
        await _show_source_modes(query, user_id)
        return

    # Source Selection
    elif data.startswith("src:"):
        await _handle_source_action(query, user_id, data)
        return

    # Destination Selection
    elif data.startswith("dst:"):
        await _handle_dest_action(query, user_id, data)
        return

    # Topic Selection
    elif data.startswith("topic:"):
        await _handle_topic_action(query, user_id, data)
        return

    # Content Filter
    elif data.startswith("content:"):
        await _handle_content_action(query, user_id, data)
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


async def _show_source_modes(query, user_id: int) -> None:
    """Show options to pick transfer source."""
    session_store.set_state(user_id, BotState.WIZARD_SOURCE_SELECT)
    text = (
        "📥 *Select Source*\n\n"
        "Choose how you would like to select the source chat or channel:"
    )
    await query.edit_message_text(
        text=text,
        reply_markup=build_chat_selection_modes_keyboard("source"),
        parse_mode="Markdown",
    )


async def _handle_source_action(query, user_id: int, data: str) -> None:
    """Handle source picking actions."""
    account_id = session_store.get_data(user_id).get("account_id")
    client = await user_client_manager.get_client_for_account(account_id)
    if not client:
        await query.edit_message_text("❌ Account session disconnected.")
        return

    if data == "src:mode:manual":
        session_store.set_state(user_id, BotState.WIZARD_SOURCE_INPUT)
        text = (
            "🆔 *Enter Source Chat ID or Username*\n\n"
            "Please send the numeric chat ID (e.g. `-1001234567890`) or public `@username`:"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="nav:new_transfer")]]
        )
        await query.edit_message_text(
            text=text, reply_markup=kb, parse_mode="Markdown"
        )
        return

    if data == "src:mode:search":
        session_store.set_state(user_id, BotState.WIZARD_SOURCE_INPUT)
        text = "🔎 *Search Source Chat*\n\nPlease send the title or keyword to search for:"
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="nav:new_transfer")]]
        )
        await query.edit_message_text(
            text=text, reply_markup=kb, parse_mode="Markdown"
        )
        return

    if data.startswith("src:cat:"):
        cat = data.split(":")[-1]
        chats = await ChatDiscovery.get_dialogs(client, filter_type=cat, limit=40)
        session_store.update_data(user_id, discovered_source_chats=chats)
        await query.edit_message_text(
            text=f"Select source {cat}:",
            reply_markup=build_chat_list_keyboard(chats, "source", page=0),
        )
        return

    if data.startswith("src:page:"):
        page = int(data.split(":")[-1])
        chats = session_store.get_data(user_id).get(
            "discovered_source_chats", []
        )
        await query.edit_message_text(
            text="Select source chat:",
            reply_markup=build_chat_list_keyboard(chats, "source", page=page),
        )
        return

    if data.startswith("src:pick:"):
        chat_id = int(data.split(":")[-1])
        # Find title
        chats = session_store.get_data(user_id).get(
            "discovered_source_chats", []
        )
        title = next((c.title for c in chats if c.id == chat_id), str(chat_id))
        session_store.update_data(
            user_id, source_chat_id=chat_id, source_chat_title=title
        )

        # Move to Destination selection
        text = (
            f"✅ *Source Selected:*\n📢 {title}\n\n"
            "Now select the *Destination*:"
        )
        await query.edit_message_text(
            text=text,
            reply_markup=build_chat_selection_modes_keyboard("dest"),
            parse_mode="Markdown",
        )
        return


async def _handle_dest_action(query, user_id: int, data: str) -> None:
    """Handle destination picking actions."""
    account_id = session_store.get_data(user_id).get("account_id")
    client = await user_client_manager.get_client_for_account(account_id)
    if not client:
        await query.edit_message_text("❌ Account session disconnected.")
        return

    if data == "dst:mode:manual":
        session_store.set_state(user_id, BotState.WIZARD_DEST_INPUT)
        text = (
            "🆔 *Enter Destination Chat ID or Username*\n\n"
            "Please send the numeric chat ID (e.g. `-1001234567890`) or public `@username`:"
        )
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="dst:back")]]
        )
        await query.edit_message_text(
            text=text, reply_markup=kb, parse_mode="Markdown"
        )
        return

    if data.startswith("dst:cat:"):
        cat = data.split(":")[-1]
        chats = await ChatDiscovery.get_dialogs(client, filter_type=cat, limit=40)
        session_store.update_data(user_id, discovered_dest_chats=chats)
        await query.edit_message_text(
            text=f"Select destination {cat}:",
            reply_markup=build_chat_list_keyboard(chats, "dest", page=0),
        )
        return

    if data.startswith("dst:page:"):
        page = int(data.split(":")[-1])
        chats = session_store.get_data(user_id).get("discovered_dest_chats", [])
        await query.edit_message_text(
            text="Select destination chat:",
            reply_markup=build_chat_list_keyboard(chats, "dest", page=page),
        )
        return

    if data.startswith("dst:pick:"):
        chat_id = int(data.split(":")[-1])
        chats = session_store.get_data(user_id).get("discovered_dest_chats", [])
        picked = next((c for c in chats if c.id == chat_id), None)
        title = picked.title if picked else str(chat_id)
        is_forum = picked.is_forum if picked else False

        session_store.update_data(
            user_id,
            destination_chat_id=chat_id,
            destination_chat_title=title,
            is_forum=is_forum,
        )

        # Detect forum supergroup
        if is_forum:
            topics = await TopicManager.get_topics(client, chat_id)
            session_store.update_data(user_id, available_topics=topics)
            text = (
                f"✅ *Destination Selected:*\n👥 {title}\n\n"
                "🧵 *This group has Topics enabled.*\n"
                "Select a destination topic:"
            )
            await query.edit_message_text(
                text=text,
                reply_markup=build_topics_keyboard(
                    topics, can_create=picked.can_manage_topics if picked else True
                ),
                parse_mode="Markdown",
            )
            return

        # Not a forum: move to Content Filter
        await _show_content_filter(query, user_id)
        return


async def _handle_topic_action(query, user_id: int, data: str) -> None:
    """Handle forum topic selection."""
    if data.startswith("topic:pick:"):
        topic_id = int(data.split(":")[-1])
        topics = session_store.get_data(user_id).get("available_topics", [])
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

    if data == "topic:create":
        session_store.set_state(user_id, BotState.WIZARD_TOPIC_CREATE)
        text = "🆕 *Create New Topic*\n\nPlease send the title for the new topic:"
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="dst:back")]]
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


async def _handle_range_action(query, user_id: int, data: str) -> None:
    """Handle range selection."""
    if data.startswith("range:pick:"):
        r_type = data.split(":")[-1]
        if r_type == "custom":
            session_store.set_state(user_id, BotState.WIZARD_RANGE_INPUT)
            text = (
                "🔢 *Enter Message ID Range*\n\n"
                "Send start and end IDs separated by space or hyphen.\n"
                "Example: `1 155` or `1-155`"
            )
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="content:done")]]
            )
            await query.edit_message_text(
                text=text, reply_markup=kb, parse_mode="Markdown"
            )
            return

        # Numeric limits: 100, 500, 1000 or all
        limit = None if r_type == "all" else int(r_type)
        session_store.update_data(
            user_id,
            range_type=r_type,
            range_limit=limit,
            start_message_id=None,
            end_message_id=None,
        )

        # Move to Duplicate handling
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
        # Show Transfer Preview!
        await _show_transfer_preview(query, user_id)
        return


async def _show_transfer_preview(query, user_id: int) -> None:
    """Format and show Section 14 Preview."""
    udata = session_store.get_data(user_id)
    source_title = udata.get("source_chat_title", "Unknown")
    dest_title = udata.get("destination_chat_title", "Unknown")
    topic_name = udata.get("topic_name")
    topic_line = f"\nTOPIC\n🧵 {topic_name}\n" if topic_name else ""

    c_types = udata.get("content_types", ["all"])
    content_str = "📦 Everything" if "all" in c_types else ", ".join(c_types)

    r_limit = udata.get("range_limit")
    s_id = udata.get("start_message_id")
    e_id = udata.get("end_message_id")
    if s_id and e_id:
        range_str = f"Messages {s_id} → {e_id}"
    elif r_limit:
        range_str = f"Last {r_limit} messages"
    else:
        range_str = "All Messages"

    dup_mode = udata.get("duplicate_mode", "skip")
    dup_str = "⏭ Skip existing" if dup_mode == "skip" else "🔄 Transfer again"

    preview_text = (
        "🚀 *Transfer Preview*\n\n"
        f"SOURCE\n📢 {source_title}\n\n"
        f"DESTINATION\n👥 {dest_title}\n"
        f"{topic_line}\n"
        f"CONTENT\n{content_str}\n\n"
        f"RANGE\n{range_str}\n\n"
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
    if data == "preview:start":
        udata = session_store.get_data(user_id)
        account_id = udata.get("account_id")
        source_id = udata.get("source_chat_id")
        source_title = udata.get("source_chat_title")
        dest_id = udata.get("destination_chat_id")
        dest_title = udata.get("destination_chat_title")
        dest_thread_id = udata.get("destination_thread_id")
        topic_name = udata.get("topic_name")
        c_types = ",".join(udata.get("content_types", ["all"]))
        dup_mode = udata.get("duplicate_mode", "skip")
        start_id = udata.get("start_message_id")
        end_id = udata.get("end_message_id")

        # Create job
        job = await transfer_manager.create_job(
            owner_id=user_id,
            telegram_account_id=account_id,
            source_chat_id=source_id,
            source_chat_title=source_title,
            destination_chat_id=dest_id,
            destination_chat_title=dest_title,
            destination_thread_id=dest_thread_id,
            topic_name=topic_name,
            content_types=c_types,
            duplicate_mode=dup_mode,
            start_message_id=start_id,
            end_message_id=end_id,
            total_messages=100,  # dynamic during scan
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

    if action == "job_retry":
        await transfer_manager.retry_failed_job(job_id)
        await query.answer("Retrying failed messages...")
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

        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Retry Failed", callback_data=f"job_retry:{job.id}"
                    )
                ],
                [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
            ]
        )
        await query.edit_message_text(
            text=detail_text, reply_markup=kb, parse_mode="Markdown"
        )
        return

