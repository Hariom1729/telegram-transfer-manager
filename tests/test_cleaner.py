"""Tests for Chat Cleaner and Duplicate Removal Service."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telegram import CallbackQuery, Message as TgMessage, Update, User as TgUser
from telegram.ext import ContextTypes
from telethon.errors import ChatAdminRequiredError, FloodWaitError

from app.bot.commands import cmd_clean, cmd_dedup
from app.bot.handlers import text_message_handler
from app.bot.states import BotState, session_store
from app.config import settings
from app.database import Base
from app.models.telegram_account import TelegramAccount
from app.transfer.cleaner import (
    ChatCleanerService,
    chat_cleaner_service,
    handle_clean_callback,
    show_clean_menu,
)


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
    from app.config import Settings
    orig_admins = Settings.ADMIN_USER_IDS
    Settings.ADMIN_USER_IDS = [12345]
    settings.ADMIN_USER_IDS = [12345]

    with patch("app.transfer.cleaner.get_session", mock_get_session), \
         patch("app.database.get_session", mock_get_session), \
         patch("app.bot.middleware.get_session", mock_get_session), \
         patch.object(Settings, "is_admin", side_effect=lambda uid: uid == 12345):
        yield
        Settings.ADMIN_USER_IDS = orig_admins
        settings.ADMIN_USER_IDS = orig_admins
        await engine.dispose()



def make_mock_update(user_id: int = 12345, text: str = ""):
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock(spec=TgUser)
    update.effective_user.id = user_id
    update.effective_user.first_name = "Admin"

    message = MagicMock(spec=TgMessage)
    message.message_id = 999
    message.text = text
    message.reply_text = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()

    update.effective_message = message
    update.callback_query = None

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    context.args = []
    context.bot = MagicMock()
    return update, context


def make_mock_callback_update(user_id: int = 12345, data: str = ""):
    update = MagicMock(spec=Update)
    update.effective_user = MagicMock(spec=TgUser)
    update.effective_user.id = user_id

    query = MagicMock(spec=CallbackQuery)
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.reply_text = AsyncMock()

    update.callback_query = query
    update.effective_message = None

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    return update, context


# =============================================================================
# 1. Cleaner Service Unit Tests
# =============================================================================

def test_extract_media_key():
    """Verify deterministic media key generation for doc, photo, and non-media."""
    # 1. Non-media message
    msg_none = SimpleNamespace(id=1, media=None)
    assert ChatCleanerService._extract_media_key(msg_none) is None

    # 2. Document message
    attr = SimpleNamespace(file_name="video.mp4")
    doc = SimpleNamespace(size=52428800, attributes=[attr])
    msg_doc = SimpleNamespace(id=2, media=True, document=doc, photo=None)
    assert ChatCleanerService._extract_media_key(msg_doc) == ("doc", 52428800, "video.mp4")

    # 3. Photo message
    photo = SimpleNamespace(id=123456789)
    msg_photo = SimpleNamespace(id=3, media=True, document=None, photo=photo)
    assert ChatCleanerService._extract_media_key(msg_photo) == ("photo", 123456789)


@pytest.mark.asyncio
async def test_scan_duplicates():
    """Verify duplicate detection identifies redundant media and text while keeping original."""
    attr1 = SimpleNamespace(file_name="movie.mkv")
    doc1 = SimpleNamespace(size=104857600, attributes=[attr1])

    # Messages in chronological order (iter_messages with reverse=True)
    messages = [
        # Original doc
        SimpleNamespace(id=1, media=True, document=doc1, photo=None, message="Movie"),
        # Duplicate doc (same size and name)
        SimpleNamespace(id=2, media=True, document=doc1, photo=None, message="Movie Copy"),
        # Original text
        SimpleNamespace(id=3, media=None, message="Welcome to the channel!"),
        # Duplicate text
        SimpleNamespace(id=4, media=None, message="Welcome to the channel!"),
        # Original photo
        SimpleNamespace(id=5, media=True, document=None, photo=SimpleNamespace(id=999), message=None),
        # Duplicate photo
        SimpleNamespace(id=6, media=True, document=None, photo=SimpleNamespace(id=999), message=None),
    ]

    async def mock_iter_messages(*args, **kwargs):
        for m in messages:
            yield m

    mock_client = MagicMock()
    mock_client.iter_messages = mock_iter_messages

    result = await ChatCleanerService.scan_duplicates(
        client=mock_client,
        chat_id=-100123456789,
        chat_title="Test Channel",
        limit=100,
    )

    assert result.total_scanned == 6
    assert result.duplicate_ids == [2, 4, 6]
    assert result.duplicate_media_count == 2
    assert result.duplicate_text_count == 1
    assert result.total_duplicate_bytes == 104857600


@pytest.mark.asyncio
async def test_scan_media_files():
    """Verify scan_media_files identifies all messages containing media."""
    messages = [
        SimpleNamespace(id=10, media=True, document=SimpleNamespace(size=1000)),
        SimpleNamespace(id=11, media=None, document=None),
        SimpleNamespace(id=12, media=True, document=SimpleNamespace(size=2000)),
    ]

    async def mock_iter_messages(*args, **kwargs):
        for m in messages:
            yield m

    mock_client = MagicMock()
    mock_client.iter_messages = mock_iter_messages

    media_ids, total_bytes = await ChatCleanerService.scan_media_files(mock_client, -1001, limit=50)
    assert media_ids == [10, 12]
    assert total_bytes == 3000


@pytest.mark.asyncio
async def test_delete_messages_batch():
    """Verify batch message deletion executes in chunks and handles FloodWait."""
    mock_client = MagicMock()
    mock_client.delete_messages = AsyncMock()

    # Generate 150 IDs to test chunking
    ids = list(range(1, 151))
    deleted = await ChatCleanerService.delete_messages_batch(mock_client, -100123, ids)

    assert deleted == 150
    # 150 items chunked at 100 = 2 calls
    assert mock_client.delete_messages.call_count == 2


@pytest.mark.asyncio
async def test_delete_messages_batch_permission_error():
    """Verify admin permission error raises clear user-friendly exception."""
    mock_client = MagicMock()
    mock_client.delete_messages = AsyncMock(side_effect=ChatAdminRequiredError(request=None))

    with pytest.raises(RuntimeError, match="admin permissions with 'Delete Messages'"):
        await ChatCleanerService.delete_messages_batch(mock_client, -100123, [1, 2, 3])


# =============================================================================
# 2. Command Handlers (/clean and /dedup)
# =============================================================================

@pytest.mark.asyncio
async def test_cmd_clean_opens_chat_picker():
    """Verify /clean initializes cleaning mode and opens source picker."""
    update, context = make_mock_update(user_id=12345, text="/clean")
    mock_acc = TelegramAccount(
        id=1,
        user_id=12345,
        phone_number="+1234567890",
        session_name="session1",
        first_name="TestUser",
    )

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])), \
         patch("app.bot.commands.ChatPicker.show_source_picker", AsyncMock()) as mock_picker:
        await cmd_clean(update, context)

        udata = session_store.get_data(12345)
        assert udata.get("is_cleaning_mode") is True
        mock_picker.assert_called_once_with(update.effective_message, 12345)


@pytest.mark.asyncio
async def test_cmd_dedup_alias_calls_clean():
    """Verify /dedup alias invokes cleaning workflow identically."""
    update, context = make_mock_update(user_id=12345, text="/dedup")
    mock_acc = TelegramAccount(
        id=1,
        user_id=12345,
        phone_number="+1234567890",
        session_name="session1",
        first_name="TestUser",
    )

    with patch("app.bot.commands.user_client_manager.list_user_accounts", AsyncMock(return_value=[mock_acc])), \
         patch("app.bot.commands.ChatPicker.show_source_picker", AsyncMock()) as mock_picker:
        await cmd_dedup(update, context)

        udata = session_store.get_data(12345)
        assert udata.get("is_cleaning_mode") is True
        mock_picker.assert_called_once()


# =============================================================================
# 3. Clean Callbacks and Workflows
# =============================================================================

@pytest.mark.asyncio
async def test_handle_clean_callback_scan_and_execute_dedup():
    """Verify scanning duplicates and executing deletion via callbacks."""
    # 1. Trigger scan_dedup
    update, _ = make_mock_callback_update(user_id=12345, data="clean:scan_dedup:-100123")
    session_store.update_data(12345, account_id=1, clean_chat_title="My Channel")

    mock_client = MagicMock()
    mock_client.is_connected = MagicMock(return_value=True)

    scan_res = SimpleNamespace(
        chat_id=-100123,
        chat_title="My Channel",
        total_scanned=20,
        duplicate_ids=[5, 10],
        duplicate_media_count=2,
        duplicate_text_count=0,
        total_duplicate_bytes=1048576,
    )

    with patch("app.transfer.cleaner.user_client_manager.get_active_client", AsyncMock(return_value=mock_client)), \
         patch("app.transfer.cleaner.chat_cleaner_service.scan_duplicates", AsyncMock(return_value=scan_res)):
        await handle_clean_callback(update, 12345, "clean:scan_dedup:-100123")

        # Check prompt edit
        text = update.callback_query.edit_message_text.call_args.kwargs["text"]
        assert "Duplicate Scan Complete" in text
        assert "Duplicates Found:* **2**" in text
        assert session_store.get_data(12345).get("pending_delete_ids") == [5, 10]

    # 2. Trigger exec_dedup
    update2, _ = make_mock_callback_update(user_id=12345, data="clean:exec_dedup:-100123")
    with patch("app.transfer.cleaner.user_client_manager.get_active_client", AsyncMock(return_value=mock_client)), \
         patch("app.transfer.cleaner.chat_cleaner_service.delete_messages_batch", AsyncMock(return_value=2)):
        await handle_clean_callback(update2, 12345, "clean:exec_dedup:-100123")

        text2 = update2.callback_query.edit_message_text.call_args.kwargs["text"]
        assert "Successfully Cleaned Duplicates" in text2
        assert "Removed **2** duplicate message(s)" in text2
        assert session_store.get_data(12345).get("pending_delete_ids") == []


@pytest.mark.asyncio
async def test_text_handler_clean_range_input_prompts_confirmation():
    """Verify entering range e.g. '10 20' prompts confirmation before deleting."""
    update, context = make_mock_update(user_id=12345, text="10 20")
    session_store.set_state(12345, BotState.CLEAN_RANGE_INPUT)
    session_store.update_data(12345, clean_chat_id=-100123, clean_chat_title="My Channel")

    await text_message_handler(update, context)

    call_args = update.effective_message.reply_text.call_args
    text = call_args[0][0] if call_args[0] else call_args.kwargs.get("text", "")
    assert "Confirm Range Purge" in text
    assert "Messages 10 to 20 (11 messages)" in text


    # Verify pending delete IDs set
    pending = session_store.get_data(12345).get("pending_delete_ids")
    assert pending == list(range(10, 21))
