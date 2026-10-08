"""Comprehensive tests for Slash Commands System, Registration, and State Management."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telegram import BotCommand, CallbackQuery, Message, Update, User as TgUser
from telegram.ext import ContextTypes

from app.bot.commands import (
    BOT_COMMANDS,
    cmd_accounts,
    cmd_cancel,
    cmd_chats,
    cmd_connect,
    cmd_disconnect,
    cmd_download,
    cmd_health,
    cmd_help,
    cmd_history,
    cmd_menu,
    cmd_pause,
    cmd_resume,
    cmd_search,
    cmd_settings,
    cmd_speedtest,
    cmd_start,
    cmd_status,
    cmd_transfer,
    cmd_unknown,
    handle_command_callback,
    register_command_handlers,
    setup_bot_commands,
)
from app.bot.states import BotState, session_store
from app.config import settings
from app.database import Base
from app.models.telegram_account import TelegramAccount
from app.models.transfer_job import JobStatus, TransferJob


@pytest.fixture(autouse=True)
async def setup_test_db_and_admin():
    """Ensure in-memory SQLite database and test admin user are configured."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    @asynccontextmanager
    async def mock_get_session():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    from app.config import Settings
    orig_admins = Settings.ADMIN_USER_IDS
    Settings.ADMIN_USER_IDS = [12345]
    settings.ADMIN_USER_IDS = [12345]

    with patch("app.bot.middleware.get_session", mock_get_session), \
         patch("app.database.get_session", mock_get_session), \
         patch("app.transfer.manager.get_session", mock_get_session), \
         patch("app.telegram.user_client.get_session", mock_get_session), \
         patch.object(Settings, "is_admin", side_effect=lambda uid: uid == 12345):
        yield
        Settings.ADMIN_USER_IDS = orig_admins
        settings.ADMIN_USER_IDS = orig_admins
        await engine.dispose()


def make_mock_update(user_id: int = 12345, text: str = "/start", args=None) -> tuple[MagicMock, MagicMock]:
    """Helper to create mock Telegram Update and Context objects."""
    update = MagicMock(spec=Update)
    user = MagicMock(spec=TgUser)
    user.id = user_id
    user.username = "test_admin"
    user.first_name = "Admin"
    update.effective_user = user

    msg = MagicMock(spec=Message)
    msg.text = text
    msg.reply_text = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    update.effective_message = msg
    update.callback_query = None

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.args = args or []
    context.bot = MagicMock()
    return update, context


@pytest.mark.asyncio
async def test_setup_bot_commands_registers_all_commands_and_menu_button():
    """Verify setup_bot_commands invokes set_my_commands with all 16 commands, scopes, and menu button."""
    app = MagicMock()
    app.bot.set_my_commands = AsyncMock()
    app.bot.delete_my_commands = AsyncMock()
    app.bot.set_chat_menu_button = AsyncMock()

    await setup_bot_commands(app)
    # Called for default scope and private chats scope
    assert app.bot.set_my_commands.call_count == 2
    app.bot.set_chat_menu_button.assert_called_once()

    cmds: list[BotCommand] = app.bot.set_my_commands.call_args[0][0]
    command_names = [c.command for c in cmds]
    expected_commands = [
        "start", "help", "menu", "accounts", "chats", "search",
        "transfer", "status", "history", "pause", "resume", "cancel",
        "download", "speedtest", "settings", "clean", "dedup", "health",
    ]
    for expected in expected_commands:
        assert expected in command_names, f"Missing command: {expected}"
    assert len(cmds) == 18



@pytest.mark.asyncio
async def test_unauthorized_user_blocked_on_commands():
    """Verify unauthorized users (not in ADMIN_USER_IDS) receive 403 authorization denial."""
    update, context = make_mock_update(user_id=99999, text="/start")

    await cmd_start(update, context)

    update.effective_message.reply_text.assert_called_once_with(
        "⛔ You are not authorized to use this bot."
    )


@pytest.mark.asyncio
async def test_cmd_start_and_cmd_menu():
    """Verify /start and /menu display dashboard and clear transient state."""
    update, context = make_mock_update(user_id=12345, text="/start")
    session_store.set_state(12345, BotState.WIZARD_SOURCE_SELECT)
    session_store.update_data(12345, temp_key="some_data")

    await cmd_start(update, context)

    # State cleared
    assert session_store.get_state(12345) == BotState.IDLE
    assert len(session_store.get_data(12345)) == 0
    # Sent dashboard
    assert update.effective_message.reply_text.call_count == 1
    args, kwargs = update.effective_message.reply_text.call_args
    assert "Telegram Transfer Manager" in kwargs["text"]

    # Test /menu
    update_menu, context_menu = make_mock_update(user_id=12345, text="/menu")
    await cmd_menu(update_menu, context_menu)
    assert update_menu.effective_message.reply_text.call_count == 1
    assert "Dashboard" in update_menu.effective_message.reply_text.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_cmd_help_displays_all_commands():
    """Verify /help shows command listings and short descriptions."""
    update, context = make_mock_update(user_id=12345, text="/help")

    await cmd_help(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "/start" in text
    assert "/transfer" in text
    assert "/speedtest" in text
    assert "/download" in text
    assert "/settings" in text


@pytest.mark.asyncio
async def test_cmd_accounts_no_accounts():
    """Verify /accounts when no accounts are connected."""
    update, context = make_mock_update(user_id=12345, text="/accounts")

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[])):
        await cmd_accounts(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "No Telegram accounts are connected" in text


@pytest.mark.asyncio
async def test_cmd_accounts_with_accounts_hides_secrets():
    """Verify /accounts displays Account 1 / Status without revealing phone, hash, or codes."""
    update, context = make_mock_update(user_id=12345, text="/accounts")
    mock_acc = MagicMock(spec=TelegramAccount)
    mock_acc.id = 101
    mock_acc.first_name = "John"
    mock_acc.username = "johndoe"
    mock_acc.phone_number = "+919876543210"
    mock_acc.is_active = True

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])):
        await cmd_accounts(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "Account 1" in text
    assert "Status: Connected" in text
    # Ensure sensitive phone number is NOT displayed
    assert "+919876543210" not in text


@pytest.mark.asyncio
async def test_cmd_connect_starts_auth_flow():
    """Verify /connect transitions user into AUTH_WAITING_PHONE state."""
    update, context = make_mock_update(user_id=12345, text="/connect")

    await cmd_connect(update, context)

    assert session_store.get_state(12345) == BotState.AUTH_WAITING_PHONE
    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "phone number with country code" in text


@pytest.mark.asyncio
async def test_cmd_disconnect_confirmation():
    """Verify /disconnect asks for confirmation before disconnecting."""
    update, context = make_mock_update(user_id=12345, text="/disconnect")
    mock_acc = MagicMock(spec=TelegramAccount)
    mock_acc.id = 101
    mock_acc.first_name = "John"
    mock_acc.is_active = True

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])):
        await cmd_disconnect(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "Disconnect Telegram Account?" in text
    assert "Are you sure you want to disconnect" in text


@pytest.mark.asyncio
async def test_cmd_chats_opens_picker():
    """Verify /chats opens the chat picker."""
    update, context = make_mock_update(user_id=12345, text="/chats")
    mock_acc = MagicMock(id=101)

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])), \
         patch("app.bot.commands.ChatPicker.show_source_picker", AsyncMock()) as mock_show:
        await cmd_chats(update, context)
        mock_show.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_search_with_args_and_without_args():
    """Verify /search with keyword calls search directly; /search without keyword prompts user."""
    mock_acc = MagicMock(id=101)

    # 1. Without args: prompts for keyword
    update, context = make_mock_update(user_id=12345, text="/search", args=[])
    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])):
        await cmd_search(update, context)

    assert session_store.get_state(12345) == BotState.WIZARD_SOURCE_INPUT
    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "Enter a channel/group name or search keyword" in text

    # 2. With args: executes search query
    update_args, context_args = make_mock_update(user_id=12345, text="/search python", args=["python"])
    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])), \
         patch("app.bot.commands.ChatPicker.handle_search_query", AsyncMock()) as mock_search:
        await cmd_search(update_args, context_args)
        mock_search.assert_called_once()
        assert mock_search.call_args.kwargs["query_text"] == "python"


@pytest.mark.asyncio
async def test_cmd_transfer_starts_workflow():
    """Verify /transfer opens chat picker when 1 account connected."""
    update, context = make_mock_update(user_id=12345, text="/transfer")
    mock_acc = MagicMock(id=101)

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])), \
         patch("app.bot.commands.ChatPicker.show_source_picker", AsyncMock()) as mock_picker:
        await cmd_transfer(update, context)
        mock_picker.assert_called_once()


@pytest.mark.asyncio
async def test_cmd_status_no_jobs_and_with_jobs():
    """Verify /status shows empty state and active job progress."""
    # 1. No jobs
    update, context = make_mock_update(user_id=12345, text="/status")
    with patch("app.bot.commands.transfer_manager.get_active_jobs", AsyncMock(return_value=[])):
        await cmd_status(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "No active transfers" in text

    # 2. With active job
    mock_job = MagicMock(spec=TransferJob)
    mock_job.id = 24
    mock_job.source_chat_title = "Course Videos"
    mock_job.destination_chat_title = "My Channel"
    mock_job.status = "running"
    mock_job.processed_messages = 80
    mock_job.total_messages = 100
    mock_job.successful_messages = 80
    mock_job.failed_messages = 0

    update2, context2 = make_mock_update(user_id=12345, text="/status")
    with patch("app.bot.commands.transfer_manager.get_active_jobs", AsyncMock(return_value=[mock_job])):
        await cmd_status(update2, context2)

    text2 = update2.effective_message.reply_text.call_args.kwargs["text"]
    assert "Transfer #24" in text2
    assert "Processed: 80/100" in text2
    assert "Success: 80" in text2


@pytest.mark.asyncio
async def test_cmd_history_displays_transfers():
    """Verify /history lists recent transfers with pagination."""
    update, context = make_mock_update(user_id=12345, text="/history")
    mock_job = MagicMock(spec=TransferJob)
    mock_job.id = 23
    mock_job.source_chat_title = "Python Course"
    mock_job.destination_chat_title = "Courses"
    mock_job.status = JobStatus.COMPLETED.value
    mock_job.processed_messages = 50
    mock_job.successful_messages = 50
    mock_job.failed_messages = 0

    with patch("app.bot.commands.transfer_manager.get_history", AsyncMock(return_value=[mock_job])):
        await cmd_history(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "Transfer History" in text
    assert "#23" in text
    assert "Python Course → Courses" in text


@pytest.mark.asyncio
async def test_cmd_pause_resume_cancel():
    """Verify /pause, /resume, and /cancel handling."""
    mock_job = MagicMock(spec=TransferJob)
    mock_job.id = 24
    mock_job.source_chat_title = "Test Channel"
    mock_job.status = JobStatus.RUNNING.value

    # /pause
    update_p, context_p = make_mock_update(user_id=12345, text="/pause")
    with patch("app.bot.commands.transfer_manager.get_active_jobs", AsyncMock(return_value=[mock_job])):
        await cmd_pause(update_p, context_p)
    text_p = update_p.effective_message.reply_text.call_args.kwargs["text"]
    assert "Select transfer to pause" in text_p

    # /resume
    mock_job.status = JobStatus.PAUSED.value
    update_r, context_r = make_mock_update(user_id=12345, text="/resume")
    with patch("app.bot.commands.transfer_manager.get_active_jobs", AsyncMock(return_value=[mock_job])):
        await cmd_resume(update_r, context_r)
    text_r = update_r.effective_message.reply_text.call_args.kwargs["text"]
    assert "Select transfer to resume" in text_r

    # /cancel confirmation
    update_c, context_c = make_mock_update(user_id=12345, text="/cancel")
    with patch("app.bot.commands.transfer_manager.get_active_jobs", AsyncMock(return_value=[mock_job])):
        await cmd_cancel(update_c, context_c)
    text_c = update_c.effective_message.reply_text.call_args.kwargs["text"]
    assert "Cancel Transfer #24?" in text_c
    assert "Are you sure" in text_c


@pytest.mark.asyncio
async def test_cmd_download_sets_local_directory():
    """Verify /download initiates controlled download targeting OS Downloads folder."""
    update, context = make_mock_update(user_id=12345, text="/download")
    mock_acc = MagicMock(id=101)

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])), \
         patch("app.bot.commands.ChatPicker.show_source_picker", AsyncMock()) as mock_picker:
        await cmd_download(update, context)
        mock_picker.assert_called_once()
        udata = session_store.get_data(12345)
        assert udata.get("is_local_download") is True
        assert udata.get("destination_chat_title") == "💾 Downloads Folder"


@pytest.mark.asyncio
async def test_cmd_speedtest_format():
    """Verify /speedtest outputs formatted transfer benchmark with workers and cryptg."""
    update, context = make_mock_update(user_id=12345, text="/speedtest")

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[])):
        await cmd_speedtest(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "Transfer Speed Test" in text
    assert "Download:" in text
    assert "Upload:" in text
    assert "Workers:" in text
    assert "Request size:" in text
    assert "cryptg:" in text


@pytest.mark.asyncio
async def test_cmd_settings_displays_configuration():
    """Verify /settings shows download workers, directory, and retry count without secrets."""
    update, context = make_mock_update(user_id=12345, text="/settings")

    await cmd_settings(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "Transfer Manager Settings" in text
    assert "Download Workers" in text
    assert "Upload Workers" in text
    assert "Download Directory" in text
    assert "Resume Downloads" in text
    # Ensure no bot token or database URL leaked
    assert settings.BOT_TOKEN not in text
    assert settings.DATABASE_URL not in text


@pytest.mark.asyncio
async def test_unknown_command_handler():
    """Verify /somethingunknown responds with helpful error message and suggestion."""
    update, context = make_mock_update(user_id=12345, text="/invalidcommand")

    await cmd_unknown(update, context)

    update.effective_message.reply_text.assert_called_once_with(
        "❓ Unknown command.\n\nUse /help to see available commands."
    )


@pytest.mark.asyncio
async def test_menu_during_active_workflow_clears_state_safely():
    """Verify /menu safely cancels transient state without killing active background jobs."""
    update, context = make_mock_update(user_id=12345, text="/menu")
    session_store.set_state(12345, BotState.WIZARD_RANGE_INPUT)
    session_store.update_data(12345, wizard_step="message_range")

    await cmd_menu(update, context)

    assert session_store.get_state(12345) == BotState.IDLE
    assert len(session_store.get_data(12345)) == 0


@pytest.mark.asyncio
async def test_cmd_health_reports_status_and_hugging_face_port():
    """Verify /health displays system uptime, operational status, and port 7860."""
    update, context = make_mock_update(user_id=12345, text="/health")

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[])):
        await cmd_health(update, context)

    text = update.effective_message.reply_text.call_args.kwargs["text"]
    assert "System Health: OK" in text
    assert "*Status:* Online & Operational" in text
    assert "*HTTP Health Endpoint:*" in text
    assert "7860" in text
    assert "Hugging Face Spaces:" in text


@pytest.mark.asyncio
async def test_health_server_serves_web_ui_and_health():
    """Verify health server serves modern web dashboard on / and health check on /health."""
    from unittest.mock import MagicMock
    from app.health_server import _handle_http_request

    # Test / request returns HTML
    reader_root = AsyncMock()
    reader_root.read.return_value = b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
    writer_root = MagicMock()
    writer_root.drain = AsyncMock()
    writer_root.wait_closed = AsyncMock()

    await _handle_http_request(reader_root, writer_root)
    # Check that writer.write was called with Content-Type: text/html
    written_data = b"".join(call.args[0] for call in writer_root.write.call_args_list)
    assert b"Content-Type: text/html" in written_data
    assert b"Transfer Manager" in written_data

    # Test /health request returns JSON
    reader_health = AsyncMock()
    reader_health.read.return_value = b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
    writer_health = MagicMock()
    writer_health.drain = AsyncMock()
    writer_health.wait_closed = AsyncMock()

    await _handle_http_request(reader_health, writer_health)
    written_health = b"".join(call.args[0] for call in writer_health.write.call_args_list)
    assert b"Content-Type: application/json" in written_health
    assert b"\"status\": \"ok\"" in written_health


