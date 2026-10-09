"""Command, Message, and Media Handlers for Bot UI."""

import logging
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from app.bot.callbacks import handle_callback_query
from app.bot.chat_picker import ChatPicker
from app.bot.commands import (
    cmd_unknown,
    register_command_handlers,
)
from app.bot.keyboards import (
    build_auth_2fa_keyboard,
    build_auth_code_keyboard,
    build_chat_list_keyboard,
    build_chat_selection_modes_keyboard,
    build_duplicate_mode_keyboard,
    build_main_menu_keyboard,
    build_topics_keyboard,
)
from app.bot.middleware import check_authorized
from app.bot.states import BotState, session_store
from app.telegram.bot_client import bot_client_helper
from app.telegram.discovery import ChatDiscovery
from app.telegram.topics import TopicManager
from app.telegram.user_client import user_client_manager
from app.utils.validators import (
    validate_chat_identifier,
    validate_message_range,
    validate_phone_number,
)

logger = logging.getLogger(__name__)


@check_authorized
async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /start command matching Section 6 specification."""
    user_id = update.effective_user.id
    session_store.clear(user_id)

    text = (
        "🤖 *Telegram Transfer Manager*\n\n"
        "Welcome!\n\n"
        "Transfer content between Telegram chats\n"
        "using a simple Telegram interface.\n\n"
        "Choose an action:"
    )
    await update.effective_message.reply_text(
        text=text,
        reply_markup=build_main_menu_keyboard(),
        parse_mode="Markdown",
    )


@check_authorized
async def cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /cancel command."""
    user_id = update.effective_user.id
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
        await update.effective_message.reply_text(
            text=text,
            reply_markup=build_accounts_keyboard(accounts),
            parse_mode="Markdown",
        )
    else:
        await update.effective_message.reply_text(
            "❌ Operation cancelled.",
            reply_markup=build_main_menu_keyboard(),
        )


@check_authorized
async def text_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle text messages across various conversation states."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    user_id = update.effective_user.id
    state = session_store.get_state(user_id)
    udata = session_store.get_data(user_id)

    # 1. MTProto Authentication: Phone Number
    if state == BotState.AUTH_WAITING_PHONE:
        is_valid, cleaned_phone = validate_phone_number(text)
        if not is_valid:
            await message.reply_text(f"❌ {cleaned_phone}\nPlease try again:")
            return

        status_msg = await message.reply_text("⏳ Requesting login code from Telegram...")
        success, msg, status = await user_client_manager.start_login(
            telegram_bot_user_id=user_id, phone_number=cleaned_phone
        )
        if not success:
            if msg == "PHONE_INVALID":
                await status_msg.edit_text(
                    "❌ The phone number you entered is invalid.\n"
                    "Please check the phone number and country code, and try again (e.g. `+1234567890`).",
                    parse_mode="Markdown",
                )
            elif msg.startswith("FLOOD_WAIT"):
                wait_sec = msg.split(":")[-1]
                await status_msg.edit_text(
                    f"⏳ Telegram rate limit reached (FloodWait).\nPlease wait {wait_sec} seconds before trying again."
                )
            else:
                await status_msg.edit_text(f"❌ Failed to request code: {msg}")
            session_store.clear(user_id)
            return

        session_store.set_state(user_id, BotState.AUTH_WAITING_CODE)
        await status_msg.edit_text(
            "📩 *Code Sent to Telegram!*\n\n"
            "Please enter the login code you received in your official Telegram app.\n\n"
            "💡 *Tip:* You can enter your code with spaces or hyphens (e.g. `1 2 3 4 5` or `12-345`) to prevent Telegram from auto-expiring it.",
            reply_markup=build_auth_code_keyboard(),
            parse_mode="Markdown",
        )
        return

    # 2. MTProto Authentication: Code
    elif state == BotState.AUTH_WAITING_CODE:
        cleaned_code = "".join(c for c in text if c.isdigit())
        if not cleaned_code:
            await message.reply_text(
                "❌ Incorrect login code.\n\nPlease enter the latest code sent by Telegram (digits only):",
                reply_markup=build_auth_code_keyboard(),
            )
            return

        status_msg = await message.reply_text("⏳ Verifying login code...")

        success, msg, acc_id = await user_client_manager.verify_code(
            telegram_bot_user_id=user_id,
            code=cleaned_code,
        )

        if not success and msg == "2FA_REQUIRED":
            session_store.set_state(user_id, BotState.AUTH_WAITING_2FA)
            await status_msg.edit_text(
                "🔐 *Your Telegram account has two-step verification enabled.*\n\n"
                "Please enter your Telegram 2FA password:",
                reply_markup=build_auth_2fa_keyboard(),
                parse_mode="Markdown",
            )
            return

        if not success and msg == "INVALID_CODE":
            await status_msg.edit_text(
                "❌ *Incorrect login code.*\n\n"
                "Please enter the latest code sent by Telegram.",
                reply_markup=build_auth_code_keyboard(),
                parse_mode="Markdown",
            )
            return

        if not success and msg == "CODE_EXPIRED":
            await status_msg.edit_text(
                "⚠️ *This login code has expired.*\n\n"
                "Please request a new code.",
                reply_markup=build_auth_code_keyboard(),
                parse_mode="Markdown",
            )
            return

        if not success and msg == "TIMEOUT":
            session_store.clear(user_id)
            await status_msg.edit_text(
                "⚠️ *Authentication session timed out.*\n\n"
                "Please select Connect Telegram Account to restart.",
                reply_markup=build_main_menu_keyboard(),
                parse_mode="Markdown",
            )
            return

        if not success:
            await status_msg.edit_text(
                f"❌ {msg}\n\nPlease try again or request a new code:",
                reply_markup=build_auth_code_keyboard(),
                parse_mode="Markdown",
            )
            return

        session_store.clear(user_id)
        await status_msg.edit_text(
            "✅ *Telegram Account Connected Successfully!*\n\n"
            "You can now create transfers using this account.",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    # 3. MTProto Authentication: 2FA Password
    elif state == BotState.AUTH_WAITING_2FA:
        password = text.strip()
        status_msg = await message.reply_text("⏳ Verifying 2FA password...")

        success, msg, acc_id = await user_client_manager.verify_code(
            telegram_bot_user_id=user_id,
            code="",
            password=password,
        )

        if not success and msg == "INVALID_2FA":
            await status_msg.edit_text(
                "❌ *Incorrect 2FA Password*\n\n"
                "The password you entered is incorrect. Please try again or send /cancel:",
                reply_markup=build_auth_2fa_keyboard(),
                parse_mode="Markdown",
            )
            return

        if not success and msg == "TIMEOUT":
            session_store.clear(user_id)
            await status_msg.edit_text(
                "⚠️ *Authentication session timed out.*\n\n"
                "Please select Connect Telegram Account to restart.",
                reply_markup=build_main_menu_keyboard(),
                parse_mode="Markdown",
            )
            return

        if not success:
            await status_msg.edit_text(
                f"❌ {msg}\n\nPlease try entering the password again or send /cancel:",
                reply_markup=build_auth_2fa_keyboard(),
                parse_mode="Markdown",
            )
            return

        session_store.clear(user_id)
        await status_msg.edit_text(
            "✅ *Telegram Account Connected Successfully with 2FA!*\n\n"
            "You can now create transfers using this account.",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    # 4. Source Input (Search or ID)
    elif state == BotState.WIZARD_SOURCE_INPUT:
        account_id = udata.get("account_id")
        if not account_id:
            accounts = await user_client_manager.list_user_accounts(user_id)
            if accounts:
                account_id = accounts[0].id
                session_store.update_data(user_id, account_id=account_id)

        prompt_msg_id = udata.get("cp_prompt_msg_id")
        try:
            await message.delete()
        except Exception:
            pass

        await ChatPicker.handle_search_query(
            message_or_query=message,
            user_id=user_id,
            target="source",
            query_text=text,
            page=0,
            edit_message_id=prompt_msg_id,
            bot=context.bot,
        )
        return

    # 5. Destination Input (Search or ID)
    elif state == BotState.WIZARD_DEST_INPUT:
        account_id = udata.get("account_id")
        if not account_id:
            accounts = await user_client_manager.list_user_accounts(user_id)
            if accounts:
                account_id = accounts[0].id
                session_store.update_data(user_id, account_id=account_id)

        prompt_msg_id = udata.get("cp_prompt_msg_id")
        try:
            await message.delete()
        except Exception:
            pass

        await ChatPicker.handle_search_query(
            message_or_query=message,
            user_id=user_id,
            target="dest",
            query_text=text,
            page=0,
            edit_message_id=prompt_msg_id,
            bot=context.bot,
        )
        return

    # 6. Topic Creation Input
    elif state == BotState.WIZARD_TOPIC_CREATE:
        account_id = udata.get("account_id")
        dest_id = udata.get("destination_chat_id")
        client = await user_client_manager.get_client_for_account(account_id)
        if not client:
            await message.reply_text("❌ Telegram account session is disconnected.")
            return

        status_msg = await message.reply_text(f"⏳ Creating topic '{text}'...")
        try:
            new_topic, refreshed_topics = await TopicManager.create_and_refresh_topic(
                client=client,
                chat_id=dest_id,
                title=text,
                account_id=account_id,
            )
            session_store.update_data(
                user_id,
                destination_thread_id=new_topic.id,
                topic_name=new_topic.title,
                available_topics=refreshed_topics,
            )
            session_store.set_state(user_id, BotState.WIZARD_TOPIC_SELECT)

            success_text = (
                f"✅ *Topic Created Successfully!*\n\n"
                f"🧵 *{new_topic.title}* (`ID: {new_topic.id}`)\n\n"
                "Select a destination topic below to continue:"
            )
            await status_msg.edit_text(
                text=success_text,
                reply_markup=build_topics_keyboard(refreshed_topics),
                parse_mode="Markdown",
            )
        except Exception as e:
            err_str = str(e)
            if "Topics aren't available" in err_str or "cannot create topics" in err_str:
                safe_err = err_str
            else:
                safe_err = "Telegram returned an error or rate limit while creating topic."

            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 Try Again", callback_data="topic:create")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="topic:back")],
                ]
            )
            await status_msg.edit_text(
                text=f"❌ *Could not create the topic.*\n\nTelegram returned:\n_{safe_err}_",
                reply_markup=kb,
                parse_mode="Markdown",
            )
        return

    # 7. Custom Range / Quantity Input (Supports direct entry from Browser as well)
    elif state in (BotState.WIZARD_RANGE_INPUT, BotState.WIZARD_BROWSE_MESSAGES):
        raw_text = text.strip()
        clean_text = raw_text.replace("#", "").strip()
        if " to " in clean_text.lower():
            clean_text = re.sub(r"(?i)\s+to\s+", "-", clean_text)
        lower_text = clean_text.lower()
        udata = session_store.get_data(user_id)

        # 1. Check for "first N" (e.g. "first 5", "first 10")
        if lower_text.startswith("first "):
            num_str = lower_text.replace("first", "").strip()
            if num_str.isdigit() and int(num_str) > 0:
                count = int(num_str)
                session_store.update_data(
                    user_id,
                    start_message_id=None,
                    end_message_id=None,
                    range_type=f"first_{count}",
                    range_limit=count,
                )
                if udata.get("destination_chat_id") is None:
                    kb = InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("➡️ Select Destination", callback_data="cp:src:to_dest")],
                            [InlineKeyboardButton("📂 Browse Messages", callback_data="browse:open")],
                        ]
                    )
                    await message.reply_text(
                        f"✅ Quantity set: First {count} messages\n\nNext step: Select the transfer destination.",
                        reply_markup=kb,
                        parse_mode="Markdown",
                    )
                else:
                    await message.reply_text(
                        f"✅ Quantity set: First {count} messages (from beginning)\n\nConfigure duplicate handling:",
                        reply_markup=build_duplicate_mode_keyboard("skip"),
                        parse_mode="Markdown",
                    )
                return

        # 2. Check for "last N" (e.g. "last 20")
        if lower_text.startswith("last "):
            num_str = lower_text.replace("last", "").strip()
            if num_str.isdigit() and int(num_str) > 0:
                count = int(num_str)
                session_store.update_data(
                    user_id,
                    start_message_id=None,
                    end_message_id=None,
                    range_type=f"last_{count}",
                    range_limit=count,
                )
                if udata.get("destination_chat_id") is None:
                    kb = InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("➡️ Select Destination", callback_data="cp:src:to_dest")],
                            [InlineKeyboardButton("📂 Browse Messages", callback_data="browse:open")],
                        ]
                    )
                    await message.reply_text(
                        f"✅ Quantity set: Last {count} messages\n\nNext step: Select the transfer destination.",
                        reply_markup=kb,
                        parse_mode="Markdown",
                    )
                else:
                    await message.reply_text(
                        f"✅ Quantity set: Last {count} messages (latest)\n\nConfigure duplicate handling:",
                        reply_markup=build_duplicate_mode_keyboard("skip"),
                        parse_mode="Markdown",
                    )
                return

        # 3. Check for specific ID range (e.g. "1240-1250", "1240 1250", "#1240-#1250")
        range_parts = [p for p in re.split(r"[\s\-]+", clean_text) if p]
        if len(range_parts) == 2 and range_parts[0].isdigit() and range_parts[1].isdigit():
            start_id, end_id = int(range_parts[0]), int(range_parts[1])
            is_valid, err = validate_message_range(start_id, end_id)
            if not is_valid:
                await message.reply_text(f"❌ {err}\nPlease try again:")
                return

            session_store.update_data(
                user_id,
                start_message_id=start_id,
                end_message_id=end_id,
                range_type="custom",
                range_limit=max(0, end_id - start_id + 1),
            )
            if udata.get("destination_chat_id") is None:
                kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("➡️ Select Destination", callback_data="cp:src:to_dest")],
                        [InlineKeyboardButton("📂 Browse Messages", callback_data="browse:open")],
                    ]
                )
                await message.reply_text(
                    f"✅ Range set: Messages {start_id} → {end_id}\n\nNext step: Select the transfer destination.",
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
            else:
                await message.reply_text(
                    f"✅ Range set: Messages {start_id} → {end_id}\n\nConfigure duplicate handling:",
                    reply_markup=build_duplicate_mode_keyboard("skip"),
                    parse_mode="Markdown",
                )
            return

        # 4. Check for single message ID (e.g. "1245", "#1245")
        if clean_text.isdigit() and int(clean_text) > 0:
            single_id = int(clean_text)
            is_valid, err = validate_message_range(single_id, single_id)
            if not is_valid:
                await message.reply_text(f"❌ {err}\nPlease try again:")
                return

            session_store.update_data(
                user_id,
                start_message_id=single_id,
                end_message_id=single_id,
                range_type="single",
                range_limit=1,
            )
            if udata.get("destination_chat_id") is None:
                kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("➡️ Select Destination", callback_data="cp:src:to_dest")],
                        [InlineKeyboardButton("📂 Browse Messages", callback_data="browse:open")],
                    ]
                )
                await message.reply_text(
                    f"✅ Single message set: Message #{single_id}\n\nNext step: Select the transfer destination.",
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
            else:
                await message.reply_text(
                    f"✅ Single message set: Message #{single_id}\n\nConfigure duplicate handling:",
                    reply_markup=build_duplicate_mode_keyboard("skip"),
                    parse_mode="Markdown",
                )
            return

        if state == BotState.WIZARD_BROWSE_MESSAGES:
            await message.reply_text(
                "💡 *Range Selection*\n\n"
                "To choose messages from the browser, send:\n"
                "• **Range:** `10-25` or `10 25`\n"
                "• **Single message:** `15`\n"
                "• **Or tap [🎯 Select Range]** below the browser.",
                parse_mode="Markdown",
            )
            return

        await message.reply_text(
            "❌ Invalid format.\n\n"
            "You can enter:\n"
            "• **Single message ID:** `1245`\n"
            "• **Specific ID range:** `1240-1250` or `1240 1250`\n"
            "• **Recent messages:** `first 10` or `last 20`"
        )
        return

    # 8. Jump to Message ID in Browser
    elif state == BotState.WIZARD_BROWSE_JUMP:
        raw_text = text.strip()
        if not raw_text.isdigit() or int(raw_text) < 1:
            await message.reply_text(
                "❌ *Invalid Message ID.*\nPlease send a valid numeric Telegram message ID (e.g. `1245`):",
                parse_mode="Markdown",
            )
            return
        jump_id = int(raw_text)
        from app.bot.message_browser import MessageBrowser
        await MessageBrowser.show_browser(message, user_id, jump_id=jump_id)
        return

    # 9. Search Messages in Browser
    elif state == BotState.WIZARD_BROWSE_SEARCH:
        search_query = text.strip()
        from app.bot.message_browser import MessageBrowser
        await MessageBrowser.show_browser(message, user_id, search_query=search_query)
        return

    # 8. Clean Range Input (for purging messages by range)
    elif state == BotState.CLEAN_RANGE_INPUT:
        parts = text.replace("-", " ").split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            start_id, end_id = int(parts[0]), int(parts[1])
            is_valid, err = validate_message_range(start_id, end_id)
            if not is_valid:
                await message.reply_text(f"❌ {err}\nPlease try again:")
                return

            chat_id = udata.get("clean_chat_id")
            chat_title = udata.get("clean_chat_title") or str(chat_id)
            topic_id = udata.get("clean_topic_id")
            topic_title = udata.get("clean_topic_title")

            message_ids = list(range(start_id, end_id + 1))
            session_store.update_data(
                user_id,
                pending_delete_ids=message_ids,
            )
            count = len(message_ids)
            topic_line = f"\n• *Topic:* 🧵 {topic_title}" if topic_id else ""
            confirm_text = (
                f"⚠️ *Confirm Range Purge*\n\n"
                f"• *Chat:* {chat_title}{topic_line}\n"
                f"• *Range:* Messages {start_id} to {end_id} ({count} messages)\n\n"
                f"Are you sure you want to permanently delete these messages?"
            )
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"💥 Yes, Delete {count} Messages",
                            callback_data=f"clean:exec_range:{chat_id}",
                        )
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="nav:home")],
                ]
            )
            await message.reply_text(confirm_text, reply_markup=kb, parse_mode="Markdown")

            return
        else:
            await message.reply_text(
                "❌ Invalid range format.\n\n"
                "Please enter start and end IDs, e.g. `1 50` or `1-50`.\n"
                "Send /cancel to abort."
            )
            return

    # Default idle fallback: Single message forwarding workflow (Section 25)
    await _prompt_single_message_forward(message, user_id)



@check_authorized
async def media_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle direct media (photo, document, video, audio) sent to the bot (Section 25)."""
    message = update.effective_message
    if not message:
        return
    user_id = update.effective_user.id
    await _prompt_single_message_forward(message, user_id)


async def _prompt_single_message_forward(message, user_id: int) -> None:
    """Prompt user where to send a direct message or media."""
    session_store.set_state(user_id, BotState.DIRECT_MSG_DEST_SELECT)
    session_store.update_data(
        user_id,
        direct_source_chat_id=message.chat_id,
        direct_source_message_id=message.message_id,
    )

    accounts = await user_client_manager.list_user_accounts(user_id)
    if not accounts:
        await message.reply_text(
            "Where should I send this?\n\n"
            "Please connect a Telegram account first via /start to discover destinations."
        )
        return

    client = await user_client_manager.get_client_for_account(accounts[0].id)
    if not client:
        return

    dialogs = await ChatDiscovery.get_dialogs(client, limit=6)
    buttons = []
    for d in dialogs:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Send to {d.title[:25]}",
                    callback_data=f"direct_send:{d.id}:{d.is_forum}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")])

    await message.reply_text(
        "Where should I send this?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@check_authorized
async def handle_direct_send_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle sending a single message/file to chosen destination."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("direct_send:"):
        return

    parts = query.data.split(":")
    dest_chat_id = int(parts[1])
    is_forum = parts[2] == "True" if len(parts) > 2 else False
    user_id = update.effective_user.id

    udata = session_store.get_data(user_id)
    src_chat_id = udata.get("direct_source_chat_id")
    src_msg_id = udata.get("direct_source_message_id")

    if not src_chat_id or not src_msg_id:
        await query.answer("Message expired. Please re-send.", show_alert=True)
        return

    accounts = await user_client_manager.list_user_accounts(user_id)
    if not accounts:
        await query.answer("No active account.", show_alert=True)
        return

    client = await user_client_manager.get_client_for_account(accounts[0].id)
    if not client:
        await query.answer("Client disconnected.", show_alert=True)
        return

    if is_forum:
        # Prompt for topic
        topics = await TopicManager.get_topics(client, dest_chat_id)
        buttons = [
            [
                InlineKeyboardButton(
                    f"🧵 {t.title}",
                    callback_data=f"direct_topic:{dest_chat_id}:{t.id}",
                )
            ]
            for t in topics[:6]
        ]
        buttons.append(
            [InlineKeyboardButton("❌ Cancel", callback_data="nav:cancel")]
        )
        await query.edit_message_text(
            "Select Topic:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # Direct send via Bot API copy
    try:
        sent_id = await bot_client_helper.send_or_copy_message(
            destination_chat_id=dest_chat_id,
            from_chat_id=src_chat_id,
            message_id=src_msg_id,
        )
        session_store.clear(user_id)
        await query.edit_message_text(
            f"🚀 Sent successfully to destination (`{dest_chat_id}`)!",
            reply_markup=build_main_menu_keyboard(),
            parse_mode="Markdown",
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Failed to send: {e}")


@check_authorized
async def handle_direct_topic_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle sending a single message/file to chosen forum topic."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("direct_topic:"):
        return

    parts = query.data.split(":")
    dest_chat_id = int(parts[1])
    topic_id = int(parts[2])
    user_id = update.effective_user.id

    udata = session_store.get_data(user_id)
    src_chat_id = udata.get("direct_source_chat_id")
    src_msg_id = udata.get("direct_source_message_id")

    try:
        sent_id = await bot_client_helper.send_or_copy_message(
            destination_chat_id=dest_chat_id,
            from_chat_id=src_chat_id,
            message_id=src_msg_id,
            thread_id=topic_id,
        )
        session_store.clear(user_id)
        await query.edit_message_text(
            f"🚀 Sent successfully to topic #{topic_id}!",
            reply_markup=build_main_menu_keyboard(),
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Failed to send to topic: {e}")


@check_authorized
async def download_benchmark_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /download_benchmark command to run internal performance benchmark."""
    user_id = update.effective_user.id
    if not settings.is_admin(user_id):
        await update.effective_message.reply_text("⛔ Admin access required for benchmarking.")
        return

    from app.transfer.downloader import fast_media_downloader

    args = context.args or []
    if len(args) < 2:
        help_text = (
            "📊 *Download Benchmark Tool*\n\n"
            "Evaluates throughput across 1, 2, 4, and 8 parallel workers.\n\n"
            "*Usage:*\n"
            "`/download_benchmark <chat_id> <message_id>`\n\n"
            "Example:\n"
            "`/download_benchmark -1001234567890 42`"
        )
        await update.effective_message.reply_text(help_text, parse_mode="Markdown")
        return

    try:
        chat_id = int(args[0])
        msg_id = int(args[1])
    except ValueError:
        await update.effective_message.reply_text("❌ Invalid format. Please provide integer chat_id and message_id.")
        return

    accounts = await user_client_manager.list_user_accounts(user_id)
    if not accounts:
        await update.effective_message.reply_text("❌ No connected Telethon account found.")
        return

    client = await user_client_manager.get_active_client(accounts[0].id)
    if not client or not client.is_connected():
        await update.effective_message.reply_text("❌ Connected Telethon account is inactive or disconnected.")
        return

    status_msg = await update.effective_message.reply_text("⏳ Fetching message and preparing benchmark...")
    try:
        msg = await client.get_messages(chat_id, ids=msg_id)
        if not msg or not msg.media:
            await status_msg.edit_text("❌ Specified message does not contain media.")
            return

        await status_msg.edit_text("🚀 Benchmarking download across 1, 2, 4, and 8 workers. Please wait...")
        result = await fast_media_downloader.benchmark_download(client, msg)
        await status_msg.edit_text(result.format_report())
    except Exception as ex:
        logger.error("Download benchmark failed: %s", ex, exc_info=True)
        await status_msg.edit_text(f"❌ Benchmark failed: {ex}")


def register_handlers(app: Application) -> None:
    """Register all bot command, callback, and message handlers with python-telegram-bot."""
    # 1. Register all slash commands and command callbacks
    register_command_handlers(app)

    # 2. Legacy admin benchmark command
    app.add_handler(CommandHandler("download_benchmark", download_benchmark_command))

    # 3. Callback handlers for direct send & navigation
    app.add_handler(
        CallbackQueryHandler(handle_direct_send_callback, pattern=r"^direct_send:")
    )
    app.add_handler(
        CallbackQueryHandler(handle_direct_topic_callback, pattern=r"^direct_topic:")
    )
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # 4. Media messages for direct file sending
    app.add_handler(
        MessageHandler(
            filters.PHOTO
            | filters.VIDEO
            | filters.AUDIO
            | filters.Document.ALL
            | filters.VOICE
            | filters.ANIMATION,
            media_message_handler,
        )
    )

    # 5. General text messages (non-commands)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler)
    )

    # 6. Fall-through unknown command handler
    app.add_handler(
        MessageHandler(filters.COMMAND, cmd_unknown)
    )

